# LuminousCart E-Commerce Platform
 
A dynamic pricing service: an LRU/LFU, TTL-backed in-memory cache in front
of SQLite, exposed via a REST API, a Tkinter desktop dashboard, and a web
dashboard. Includes a native C++ latency benchmark and a pytest suite.
 
**How it works:** the web dashboard and the Tkinter GUI both talk to the
same REST API. Every price request goes through the cache engine first —
if the product is cached and not expired, it returns instantly. If not,
the engine falls back to SQLite, saves the result in the cache, and
evicts an old entry (LRU or LFU, your choice) if the cache is full.
 
## Highlights
 
- Switchable **LRU / LFU** eviction with per-entry TTL expiration
- **Flash-sale price overrides** — instant, with async persistence to SQLite
- **Cart checkout** — stock validation + atomic stock decrement
- **REST API** (stdlib only, no Flask) with a token-bucket rate limiter
- **Telemetry** — hit ratio, p50/p95/p99 latency
- **Tkinter GUI** and a standalone **web dashboard**
- **Native C++ benchmark** for raw latency comparison
- Self-seeds its own database on first run — zero setup
## Tech
 
Python (stdlib only) · SQLite · Tkinter · C++17 · REST/HTTP · pytest
 
## Quickstart
 
```bash
python3 app.py
```
 
Creates `products.db` if missing (1,000 seeded products), starts the API
on port `8000` (or next free port), and opens the desktop GUI.
 
For the web dashboard, open `index.html` in a browser while `app.py` is
running (update `API_BASE` in `index.html` if the port isn't 8000).
 
## API
 
| Method | Endpoint | Description |
|---|---|---|
| GET | `/api/v1/products/{id}` | Get a product's price (cache-backed) |
| GET | `/api/v1/checkout?cart=1:2,5:1` | Checkout a cart (`id:qty` pairs) |
| GET | `/api/v1/cache/stats` | Hit ratio + latency stats |
 
```bash
curl http://localhost:8000/api/v1/products/5
# {"product_id": 5, "price": 213.47, "source": "SQLITE_MISS", "latency_ms": 0.42}
```
 
## Tests
 
```bash
pip install pytest
pytest test_flash_sale.py -v
```
 
Covers cache hit/miss, LRU/LFU eviction, TTL expiration, flash overrides,
and the rate limiter.
 
## C++ benchmark
 
```bash
g++ -O3 -std=c++17 engine_perf.cpp -o engine_perf && ./engine_perf
```
 
Also runnable from the GUI's "C++ Engine" tab.
 
## Files
 
| File | Purpose |
|---|---|
| `app.py` | Cache engine, REST API, rate limiter, Tkinter GUI |
| `products.db` | SQLite DB (`id`, `price`, `stock`) — auto-created |
| `index.html` | Web dashboard |
| `engine_perf.cpp` | Native latency benchmark |
| `test_flash_sale.py` | pytest suite |
| `setup_db.py` | Optional: regenerate a fresh DB (not required — `app.py` self-seeds) |
 
