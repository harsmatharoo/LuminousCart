# Python Analytics + C++ Cache

A working version of the "Python asks C++, C++ asks SQL" pattern from the
diagram. 3 files, no external dependencies beyond a C++ compiler and
Python's standard library.

```
Python (benchmark.py) --ctypes call--> C++ (libcache.so) --SQL, only on miss--> SQLite (products.db)
```

## Files

| File               | What it is                                                          |
|--------------------|----------------------------------------------------------------------|
| `setup_db.py`      | Creates `products.db`, a `products` table, 1000 fake rows            |
| `cache_engine.cpp` | The C++ cache: `unordered_map<int,double>` in front of SQLite        |
| `build.sh`         | Compiles `cache_engine.cpp` into `libcache.so`                       |
| `benchmark.py`     | Times 10,000 direct-SQL lookups vs 10,000 through-cache lookups      |

## How to run it (3 commands)

```bash
python3 setup_db.py     # 1. build the database
./build.sh               # 2. compile the C++ cache into libcache.so
python3 benchmark.py     # 3. run the race
```

If step 2 fails with `sqlite3.h: No such file or directory`, install the
dev headers first: `sudo apt-get install libsqlite3-dev`.

## Actual measured output (this machine, just now)

```
Path A: querying SQLite directly...
  Done in 0.0396s
Path B: querying through the C++ cache...
  Done in 0.0035s  (cache hits: 9,999, misses: 1)

SQL took       0.0396 seconds.
C++ Cache took 0.0035 seconds.
Speedup: 11.2x
```

Reran it 3 more times — speedup lands consistently in the 11-13x range.

**Honest note on the numbers:** the prompt's example said "84x." I'm not
going to fake a bigger number than what actually happened. On a 1,000-row
local SQLite file, SQLite itself is already very fast (it's mostly served
from OS page cache), so the gap between "fast" and "instant" is real but
not dramatic. You'd see a much bigger multiplier — 100x, 1000x+ — with a
DB over a real network, or a bigger/more expensive query. If you want to
make the demo more dramatic without lying about the numbers, see "Making
the speedup bigger" below.

## How `cache_engine.cpp` actually works (plain English)

1. Python calls `cache_get_price(5)` through ctypes — this is a direct
   function call into the compiled library, not a subprocess or a socket,
   so there's almost no overhead getting into C++ code.
2. Inside, we check `cache.find(id)` — a hash map lookup, O(1) average
   case, nanoseconds.
3. **Miss** (first time only): run
   `SELECT price FROM products WHERE id = ?` against `products.db` using
   the real SQLite C API (`sqlite3_prepare_v2` / `sqlite3_step`), grab the
   price, store it in the map (`cache[id] = price`), return it.
4. **Hit** (every time after): skip SQL entirely, return straight from the
   map.

The map and the open SQLite connection both live in global/static state
inside the `.so` — they persist across calls as long as the Python process
that loaded the library stays alive, which is exactly what makes the
"instant" path work.

## Why ctypes instead of a socket or a subprocess

Two other ways to connect Python to a C++ program are (a) spawn it as a
subprocess and talk over stdin/stdout, or (b) run it as a server and talk
over a TCP socket. Both work, but both add real overhead — process
scheduling, pipe I/O, or network stack round-trips — per call. That
overhead would swamp the microsecond-scale hash map lookup we're trying to
show off. Compiling to a shared library and calling it with `ctypes` skips
all of that: it's the same cost as calling a function that lives in your
own process.

## Making the speedup bigger (optional)

If you want a bigger number for a demo/interview:
- Make the SQL query artificially heavier (add a `JOIN`, a `LIKE` scan
  over a bigger table, an `ORDER BY`) so the "miss" path costs more and
  the cache's advantage compounds over more misses on a wider ID range.
- Point `cache_init` at a database on an actual remote server instead of
  a local file — network round-trip latency (even 1-5ms) dwarfs a hash
  map lookup and easily gets you into the 100x-1000x range.
- Increase `ITERATIONS` in `benchmark.py` and query a random ID from a
  small "hot set" each time instead of always id `5`, so you're
  demonstrating a realistic cache with a mix of hits and occasional
  misses rather than 1 miss out of 10,000.
