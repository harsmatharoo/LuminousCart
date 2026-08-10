"""
setup_db.py
-----------
Creates products.db (a plain SQLite file, no server needed) and fills it
with a `products` table: id, name, price.

Run this once before the benchmark:
    python3 setup_db.py
"""

import sqlite3
import os
import random

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "products.db")


def main():
    # Start clean every time this is run, so re-running never errors out
    # or leaves duplicate rows behind.
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE products (
            id    INTEGER PRIMARY KEY,
            name  TEXT NOT NULL,
            price REAL NOT NULL
        )
        """
    )

    # 1000 fake products so the table isn't trivially tiny.
    random.seed(42)  # deterministic, so results are reproducible
    rows = [
        (i, f"Product-{i}", round(random.uniform(1.0, 500.0), 2))
        for i in range(1, 1001)
    ]
    cur.executemany("INSERT INTO products (id, name, price) VALUES (?, ?, ?)", rows)

    conn.commit()
    conn.close()

    print(f"Created {DB_PATH} with {len(rows)} products.")
    print("Example row: id=5 ->", rows[4])


if __name__ == "__main__":
    main()
