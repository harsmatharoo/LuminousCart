import os
import time
import tempfile
import sqlite3
import pytest

from app import ProductionCacheEngine, TokenBucketRateLimiter

@pytest.fixture
def temp_db():
    """Creates a temporary SQLite database with dummy products for isolated testing."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("""
        CREATE TABLE products (
            id INTEGER PRIMARY KEY,
            price REAL NOT NULL
        )
    """)
    # Insert 5 test items
    conn.executemany(
        "INSERT INTO products (id, price) VALUES (?, ?)",
        [(1, 10.0), (2, 20.0), (3, 30.0), (4, 40.0), (5, 50.0)]
    )
    conn.commit()
    conn.close()
    
    yield path
    
    if os.path.exists(path):
        os.remove(path)


def test_cache_miss_and_hit(temp_db):
    """Verifies that the first read is a SQLite miss, and subsequent reads are cache hits."""
    engine = ProductionCacheEngine(temp_db, capacity=10, ttl_seconds=30.0)
    
    # First request: should be a miss from SQLite
    price, source, latency = engine.get_price(1)
    assert price == 10.0
    assert source == "SQLITE_MISS"
    
    # Second request: should pull straight from the LRU cache
    price_cached, source_cached, latency_cached = engine.get_price(1)
    assert price_cached == 10.0
    assert source_cached == "CACHE_HIT_LRU"
    
    stats = engine.stats()
    assert stats["hits"] == 1
    assert stats["misses"] == 1
    assert stats["hit_ratio"] == 50.0


def test_lru_eviction_policy(temp_db):
    """Ensures that LRU correctly evicts the least recently accessed item when full."""
    # Set capacity to 2 items only
    engine = ProductionCacheEngine(temp_db, capacity=2, ttl_seconds=30.0)
    engine.set_policy("LRU")
    
    # Fill cache with item 1 and item 2
    engine.get_price(1)
    engine.get_price(2)
    
    # Access item 1 again, making item 2 the oldest (least recently used)
    engine.get_price(1)
    
    # Add item 3, which should force an eviction of item 2 (since 1 was just touched)
    engine.get_price(3)
    
    # Item 1 should still be a cache hit
    _, source_1, _ = engine.get_price(1)
    assert source_1 == "CACHE_HIT_LRU"
    
    # Item 2 should have been evicted and result in a fresh SQLite miss
    _, source_2, _ = engine.get_price(2)
    assert source_2 == "SQLITE_MISS"


def test_lfu_eviction_policy(temp_db):
    """Ensures that LFU correctly evicts the least frequently requested item."""
    engine = ProductionCacheEngine(temp_db, capacity=2, ttl_seconds=30.0)
    engine.set_policy("LFU")
    
    # Populate cache
    engine.get_price(1)
    engine.get_price(2)
    
    # Access item 1 multiple times to bump its frequency count
    engine.get_price(1)
    engine.get_price(1)
    
    # Access item 2 only once more
    engine.get_price(2)
    
    # Introduce item 3, forcing an eviction. Item 2 has lower frequency than item 1.
    engine.get_price(3)
    
    # Item 1 should remain in cache due to higher frequency
    _, source_1, _ = engine.get_price(1)
    assert source_1 == "CACHE_HIT_LFU"


def test_ttl_expiration(temp_db):
    """Verifies that items expire past their TTL window and trigger a database refresh."""
    # Set an ultra-short TTL of 0.1 seconds
    engine = ProductionCacheEngine(temp_db, capacity=10, ttl_seconds=0.1)
    
    # Prime cache
    engine.get_price(1)
    _, source_first, _ = engine.get_price(1)
    assert source_first == "CACHE_HIT_LRU"
    
    # Sleep to allow TTL to lapse
    time.sleep(0.2)
    
    # Next read should register as a miss because data expired
    _, source_expired, _ = engine.get_price(1)
    assert source_expired == "SQLITE_MISS"


def test_flash_price_override(temp_db):
    """Tests that active flash overrides instantly return the override price."""
    engine = ProductionCacheEngine(temp_db, capacity=10, ttl_seconds=30.0)
    
    # Set an override for product ID 1
    engine.set_price_override(product_id=1, new_price=99.99, persist=False)
    
    price, source, _ = engine.get_price(1)
    assert price == 99.99
    assert source == "FLASH_OVERRIDE"


def test_token_bucket_rate_limiter():
    """Verifies that the rate limiter permits valid bursts and throttles excess calls."""
    # Capacity of 2 tokens, very slow refill rate
    limiter = TokenBucketRateLimiter(capacity=2, refill_rate=0.1)
    
    # First two requests should pass
    assert limiter.allow_request() is True
    assert limiter.allow_request() is True
    
    # Third immediate request should be throttled/rejected
    assert limiter.allow_request() is False