"""
app.py
------
LuminousCart Commerce Engine: High-Throughput Microsecond Dynamic Pricing Engine & E-Commerce Core.
Polyglot Edition with Auto-Setup, SQLite WAL Concurrency, Cart, and GUI Checkout.
"""

import json
import os
import queue
import random
import sqlite3
import subprocess
import threading
import time
import tkinter as tk
from collections import OrderedDict, defaultdict
from http.server import BaseHTTPRequestHandler, HTTPServer
from tkinter import messagebox, ttk
from urllib.parse import urlparse, parse_qs
import socket

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "products.db")
CPP_PATH = os.path.join(HERE, "engine_perf.cpp")


# --- AUTO-SETUP & MIGRATION UTILITIES ---
def ensure_environment():
    """Generates dummy data, ensures schema is updated, and sets up C++ stubs."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    
    # Create table if it doesn't exist
    conn.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY,
            price REAL NOT NULL,
            stock INTEGER NOT NULL DEFAULT 50
        )
    """)
    
    # Safe migration: ensure 'stock' column exists if an older DB version is present
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(products);")
    columns = [col[1] for col in cursor.fetchall()]
    if "stock" not in columns:
        conn.execute("ALTER TABLE products ADD COLUMN stock INTEGER NOT NULL DEFAULT 50;")
        conn.commit()

    # Check if table is empty
    cursor.execute("SELECT COUNT(*) FROM products;")
    count = cursor.fetchone()[0]
    if count == 0:
        conn.executemany(
            "INSERT INTO products (id, price, stock) VALUES (?, ?, ?)",
            [(i, round(random.uniform(5.0, 500.0), 2), random.randint(10, 100)) for i in range(1, 1001)]
        )
        conn.commit()
    conn.close()

    if not os.path.exists(CPP_PATH):
        stub = """#include <iostream>
#include <vector>
#include <chrono>

class TelemetryRingBuffer {
    std::vector<double> buffer;
    size_t head = 0;
    size_t max_size;
public:
    TelemetryRingBuffer(size_t size) : max_size(size) { buffer.resize(size, 0.0); }
    void push(double val) {
        buffer[head] = val;
        head = (head + 1) % max_size;
    }
};

int main() {
    std::cout << "[Native Subsystem] Engine initialized." << std::endl;
    auto start = std::chrono::high_resolution_clock::now();
    
    TelemetryRingBuffer rb(1024);
    for(int i=0; i<10000; ++i) rb.push(i * 1.5);
    
    auto end = std::chrono::high_resolution_clock::now();
    std::chrono::duration<double, std::milli> ms = end - start;
    std::cout << "[Native Subsystem] Ring buffer stress test complete in " << ms.count() << " ms." << std::endl;
    return 0;
}
"""
        with open(CPP_PATH, "w") as f:
            f.write(stub)


def find_available_port(start_port=8000):
    port = start_port
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(('0.0.0.0', port)) != 0:
                return port
        port += 1


# --- TOKEN BUCKET RATE LIMITER ---
class TokenBucketRateLimiter:
    def __init__(self, capacity: int = 100, refill_rate: float = 50.0):
        self.capacity = capacity
        self.refill_rate = refill_rate
        self.tokens = capacity
        self.last_refill = time.perf_counter()
        self.lock = threading.Lock()

    def allow_request(self) -> bool:
        with self.lock:
            now = time.perf_counter()
            elapsed = now - self.last_refill
            self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
            self.last_refill = now
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return True
            return False


# --- DUAL-POLICY CACHE ENGINE + E-COMMERCE CHECKOUT ---
class ProductionCacheEngine:
    def __init__(self, db_path: str, capacity: int = 200, ttl_seconds: float = 30.0):
        self.db_path = db_path
        self.capacity = capacity
        self.ttl = ttl_seconds
        self.policy = "LRU"

        self.cache = {}
        self.timestamps = {}
        self.frequencies = defaultdict(int)
        self.access_order = OrderedDict()

        self.overrides = {}
        self.hits = 0
        self.misses = 0
        self.latencies_ns = []

        self.lock = threading.Lock()
        self.write_queue = queue.Queue()

        threading.Thread(target=self._async_db_writer, daemon=True).start()

    def set_policy(self, policy_name: str):
        with self.lock:
            self.policy = policy_name
            self.cache.clear()
            self.timestamps.clear()
            self.frequencies.clear()
            self.access_order.clear()

    def _evict_if_needed(self):
        if len(self.cache) <= self.capacity:
            return

        if self.policy == "LRU":
            evict_pid, _ = self.access_order.popitem(last=False)
        else:
            evict_pid = min(self.frequencies, key=self.frequencies.get)
            del self.frequencies[evict_pid]
            if evict_pid in self.access_order:
                del self.access_order[evict_pid]

        self.cache.pop(evict_pid, None)
        self.timestamps.pop(evict_pid, None)

    def get_price(self, product_id: int) -> tuple[float, str, float]:
        start_ns = time.perf_counter_ns()
        now = time.time()

        with self.lock:
            if product_id in self.overrides:
                elapsed_ms = (time.perf_counter_ns() - start_ns) / 1_000_000
                self._record_latency(time.perf_counter_ns() - start_ns)
                return self.overrides[product_id], "FLASH_OVERRIDE", round(elapsed_ms, 4)

            if product_id in self.cache:
                if now < self.timestamps[product_id]:
                    self.hits += 1
                    self.frequencies[product_id] += 1
                    self.access_order.move_to_end(product_id)
                    elapsed_ms = (time.perf_counter_ns() - start_ns) / 1_000_000
                    self._record_latency(time.perf_counter_ns() - start_ns)
                    return self.cache[product_id], f"CACHE_HIT_{self.policy}", round(elapsed_ms, 4)
                else:
                    del self.cache[product_id]
                    del self.timestamps[product_id]
                    self.access_order.pop(product_id, None)

            self.misses += 1

        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        cur = conn.cursor()
        cur.execute("SELECT price FROM products WHERE id = ?", (product_id,))
        row = cur.fetchone()
        conn.close()

        if not row:
            elapsed_ms = (time.perf_counter_ns() - start_ns) / 1_000_000
            return -1.0, "NOT_FOUND", round(elapsed_ms, 4)

        price = row[0]

        with self.lock:
            self.cache[product_id] = price
            self.timestamps[product_id] = now + self.ttl
            self.frequencies[product_id] += 1
            self.access_order[product_id] = True
            self.access_order.move_to_end(product_id)
            self._evict_if_needed()

            elapsed_ms = (time.perf_counter_ns() - start_ns) / 1_000_000
            self._record_latency(time.perf_counter_ns() - start_ns)
            return price, "SQLITE_MISS", round(elapsed_ms, 4)

    def checkout_cart(self, cart_items: dict) -> tuple[bool, str, float]:
        """Safely processes checkout out-of-lock to prevent UI/Server deadlocks."""
        total_price = 0.0
        resolved_prices = {}

        for product_id_str, quantity in cart_items.items():
            try:
                pid = int(product_id_str)
                qty = int(quantity)
            except ValueError:
                return False, "Invalid product ID or quantity format.", 0.0

            price, source, _ = self.get_price(pid)
            if price < 0:
                return False, f"Product ID {pid} not found.", 0.0
            resolved_prices[pid] = (price, qty)
            total_price += price * qty

        try:
            conn = sqlite3.connect(self.db_path, timeout=10.0)
            conn.execute("PRAGMA journal_mode=WAL;")
            cur = conn.cursor()
            
            for pid, (price, qty) in resolved_prices.items():
                cur.execute("SELECT stock FROM products WHERE id = ?", (pid,))
                row = cur.fetchone()
                if not row or row[0] < qty:
                    conn.close()
                    stock_avail = row[0] if row else 0
                    return False, f"Insufficient stock for ID {pid} (Requested: {qty}, Available: {stock_avail}).", 0.0
                
                cur.execute("UPDATE products SET stock = stock - ? WHERE id = ?", (qty, pid))
            
            conn.commit()
            conn.close()
            return True, "Order placed successfully!", round(total_price, 2)
        except Exception as e:
            return False, f"Checkout failed: {str(e)}", 0.0

    def set_price_override(self, product_id: int, new_price: float, persist: bool = False):
        with self.lock:
            self.overrides[product_id] = new_price
        if persist:
            self.write_queue.put((product_id, new_price))

    def clear_overrides(self):
        with self.lock:
            self.overrides.clear()

    def _record_latency(self, latency_ns: int):
        self.latencies_ns.append(latency_ns)
        if len(self.latencies_ns) > 5000:
            self.latencies_ns = self.latencies_ns[-5000:]

    def _async_db_writer(self):
        while True:
            pid, new_price = self.write_queue.get()
            try:
                conn = sqlite3.connect(self.db_path, timeout=10.0)
                conn.execute("PRAGMA journal_mode=WAL;")
                cur = conn.cursor()
                cur.execute("UPDATE products SET price = ? WHERE id = ?", (new_price, pid))
                conn.commit()
                conn.close()
            except Exception as e:
                print(f"Async Write Error: {e}")
            finally:
                self.write_queue.task_done()

    def stats(self) -> dict:
        with self.lock:
            total = self.hits + self.misses
            ratio = (self.hits / total * 100) if total > 0 else 0.0

            if self.latencies_ns:
                sorted_lats = sorted(self.latencies_ns)
                p50 = round(sorted_lats[int(len(sorted_lats) * 0.50)] / 1_000_000, 4)
                p95 = round(sorted_lats[int(len(sorted_lats) * 0.95)] / 1_000_000, 4)
                p99 = round(sorted_lats[int(len(sorted_lats) * 0.99)] / 1_000_000, 4)
            else:
                p50, p95, p99 = 0.0, 0.0, 0.0

            return {
                "hits": self.hits,
                "misses": self.misses,
                "total_requests": total,
                "hit_ratio": round(ratio, 2),
                "active_flash_sales": len(self.overrides),
                "policy": self.policy,
                "p50_ms": p50,
                "p95_ms": p95,
                "p99_ms": p99,
            }

cache_engine = None
rate_limiter = TokenBucketRateLimiter(capacity=100, refill_rate=50.0)

# --- REST API SERVER ---
class RESTApiHandler(BaseHTTPRequestHandler):
    def _send_json(self, status_code: int, data: dict):
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("X-RateLimit-Limit", str(rate_limiter.capacity))
        self.send_header("X-RateLimit-Remaining", str(int(max(0.0, rate_limiter.tokens))))
        self.end_headers()
        self.wfile.write(json.dumps(data).encode("utf-8"))

    def do_GET(self):
        if not rate_limiter.allow_request():
            self._send_json(429, {"error": "Rate limit exceeded. Too many requests."})
            return

        parsed = urlparse(self.path)
        path = parsed.path
        query_params = parse_qs(parsed.query)

        if path == "/":
            self._send_json(200, {
                "service": "LuminousCart Commerce Engine API",
                "endpoints": {
                    "check_price": "/api/v1/products/{id}",
                    "checkout": "/api/v1/checkout?cart=1:2,3:1",
                    "cache_stats": "/api/v1/cache/stats"
                }
            })
        elif path.startswith("/api/v1/products/"):
            try:
                product_id = int(path.split("/")[-1])
                price, source, latency_ms = cache_engine.get_price(product_id)
                if price < 0:
                    self._send_json(404, {"error": "Product not found in database"})
                else:
                    self._send_json(200, {"product_id": product_id, "price": price, "source": source, "latency_ms": latency_ms})
            except ValueError:
                self._send_json(400, {"error": "Invalid product ID. Must be a number."})
        elif path == "/api/v1/checkout":
            try:
                cart_str = query_params.get("cart", [""])[0]
                if not cart_str:
                    self._send_json(400, {"error": "Cart is empty. Pass items like ?cart=1:2,3:1"})
                    return

                cart_items = {}
                for item in cart_str.split(","):
                    pid, qty = item.split(":")
                    cart_items[pid.strip()] = int(qty.strip())

                success, message, total = cache_engine.checkout_cart(cart_items)
                if success:
                    self._send_json(200, {"status": "success", "message": message, "order_total": total})
                else:
                    self._send_json(400, {"status": "error", "message": message})
            except Exception as e:
                self._send_json(400, {"error": "Invalid cart format. Use ?cart=id:qty,id:qty"})
        elif path == "/api/v1/cache/stats":
            self._send_json(200, cache_engine.stats())
        else:
            self._send_json(404, {"error": "Endpoint Not Found"})

    def log_message(self, format, *args):
        pass

def start_web_server(port: int):
    server = HTTPServer(("0.0.0.0", port), RESTApiHandler)
    server.serve_forever()

# --- DESKTOP DASHBOARD GUI ---
class FlashSaleManagerApp(tk.Tk):
    def __init__(self, port: int):
        super().__init__()
        self.port = port
        self.title("LuminousCart Commerce Engine")
        self.geometry("860x700")
        self.configure(bg="#0f172a")

        style = ttk.Style()
        style.theme_use("default")
        style.configure("TNotebook", background="#0f172a", borderwidth=0)
        style.configure("TNotebook.Tab", background="black", foreground="white", padding=[16, 10], font=("Helvetica", 11, "bold"))
        style.map("TNotebook.Tab", background=[("selected", "#0284c7")])

        tk.Label(self, text="⋆. ̊⟡ ࣪ ˖ ・ LuminousCart Commerce Platform", font=("Menlo", 26, "bold"), fg="#38bdf8", bg="#0f172a").pack(pady=(15, 0))
        tk.Label(self, text="Manage high-speed database caching, live flash sales, and cart checkouts", font=("Helvetica", 11), fg="#94a3b8", bg="#0f172a").pack(pady=(0, 15))

        notebook = ttk.Notebook(self)
        notebook.pack(expand=True, fill="both", padx=20, pady=5)

        self.tab_operations = tk.Frame(notebook, bg="#1e293b")
        self.tab_telemetry = tk.Frame(notebook, bg="#1e293b")
        self.tab_cpp = tk.Frame(notebook, bg="#1e293b")
        self.tab_help = tk.Frame(notebook, bg="#1e293b")

        notebook.add(self.tab_operations, text=" Pricing & Ops")
        notebook.add(self.tab_telemetry, text=" Telemetry & Cache")
        notebook.add(self.tab_cpp, text=" C++ Engine")
        notebook.add(self.tab_help, text=" How to Use")

        self.build_operations_tab()
        self.build_telemetry_tab()
        self.build_cpp_tab()
        self.build_help_tab()

        tk.Label(self, text=f" Local API Online: http://localhost:{self.port}", fg="#38bdf8", bg="#0f172a", font=("Helvetica", 10, "bold")).pack(pady=10)
        self.update_telemetry()

    def build_operations_tab(self):
        f_inspect = tk.LabelFrame(self.tab_operations, text="  1. Check Current Database Price & Stock ", fg="#e2e8f0", bg="#1e293b", bd=1, font=("Helvetica", 11, "bold"))
        f_inspect.pack(fill="x", padx=15, pady=12)

        tk.Label(f_inspect, text="Product ID:", fg="#f8fafc", bg="#1e293b", font=("Helvetica", 10)).grid(row=0, column=0, padx=12, pady=12)
        self.entry_inspect_id = tk.Entry(f_inspect, bg="#0f172a", fg="yellow", insertbackground="white", font=("Helvetica", 11), width=8)
        self.entry_inspect_id.insert(0, "5")
        self.entry_inspect_id.grid(row=0, column=1)
        tk.Button(f_inspect, text=" Search", bg="#0284c7", fg="white", font=("Helvetica", 10, "bold"), padx=8, pady=4, command=self.on_inspect).grid(row=0, column=2, padx=15)
        self.lbl_inspect_res = tk.Label(f_inspect, text="Result: --", fg="#4ade80", bg="#1e293b", font=("Helvetica", 11, "bold"))
        self.lbl_inspect_res.grid(row=0, column=3, padx=10)

        f_override = tk.LabelFrame(self.tab_operations, text=" 2. Create Instant Flash Sale ", fg="#e2e8f0", bg="#1e293b", bd=1, font=("Helvetica", 11, "bold"))
        f_override.pack(fill="x", padx=15, pady=8)

        tk.Label(f_override, text="Product ID:", fg="#f8fafc", bg="#1e293b", font=("Helvetica", 10)).grid(row=0, column=0, padx=12, pady=12)
        self.entry_override_id = tk.Entry(f_override, bg="#0f172a", fg="#f8fafc", insertbackground="white", font=("Helvetica", 11), width=6)
        self.entry_override_id.insert(0, "5")
        self.entry_override_id.grid(row=0, column=1)

        tk.Label(f_override, text="New Price ($):", fg="#f8fafc", bg="#1e293b", font=("Helvetica", 10)).grid(row=0, column=2, padx=10)
        self.entry_override_price = tk.Entry(f_override, bg="#0f172a", fg="#f8fafc", insertbackground="white", font=("Helvetica", 11), width=8)
        self.entry_override_price.insert(0, "9.99")
        self.entry_override_price.grid(row=0, column=3)

        self.var_persist = tk.BooleanVar(value=True)
        tk.Checkbutton(f_override, text="Persist", variable=self.var_persist, fg="#cbd5e1", bg="#1e293b", selectcolor="#0f172a", font=("Helvetica", 10)).grid(row=0, column=4, padx=10)

        tk.Button(f_override, text=" Push Live", bg="#d97706", fg="white", font=("Helvetica", 10, "bold"), padx=8, pady=4, command=self.on_push_override).grid(row=0, column=5, padx=6)
        tk.Button(f_override, text=" Clear", bg="#dc2626", fg="white", font=("Helvetica", 10, "bold"), padx=8, pady=4, command=self.on_clear_overrides).grid(row=0, column=6, padx=6)

        # --- E-COMMERCE CHECKOUT PANEL ---
        f_checkout = tk.LabelFrame(self.tab_operations, text="  3. Simulate E-Commerce Cart Checkout ", fg="#e2e8f0", bg="#1e293b", bd=1, font=("Helvetica", 11, "bold"))
        f_checkout.pack(fill="x", padx=15, pady=12)

        tk.Label(f_checkout, text="Cart Format (ID:Qty, ID:Qty):", fg="#f8fafc", bg="#1e293b", font=("Helvetica", 10)).grid(row=0, column=0, padx=12, pady=12)
        self.entry_cart = tk.Entry(f_checkout, bg="#0f172a", fg="#f8fafc", insertbackground="white", font=("Helvetica", 11), width=22)
        self.entry_cart.insert(0, "1:2,5:1")
        self.entry_cart.grid(row=0, column=1, padx=8)

        tk.Button(f_checkout, text="🛒 Checkout Cart", bg="#16a34a", fg="white", font=("Helvetica", 10, "bold"), padx=10, pady=4, command=self.on_gui_checkout).grid(row=0, column=2, padx=12)
        self.lbl_checkout_res = tk.Label(f_checkout, text="Status: Ready", fg="#38bdf8", bg="#1e293b", font=("Helvetica", 11, "bold"))
        self.lbl_checkout_res.grid(row=1, column=0, columnspan=3, padx=12, pady=8, sticky="w")

    def build_telemetry_tab(self):
        f_controls = tk.LabelFrame(self.tab_telemetry, text="  Cache Memory Behavior ", fg="#e2e8f0", bg="#1e293b", bd=1, font=("Helvetica", 11, "bold"))
        f_controls.pack(fill="x", padx=15, pady=15)
        
        tk.Label(f_controls, text="Eviction Policy:", fg="#94a3b8", bg="red", font=("Helvetica", 10, "bold")).pack(anchor="w", padx=12, pady=(6,0))

        self.var_policy = tk.StringVar(value="LRU")
        tk.Radiobutton(f_controls, text="LRU (Deletes oldest used)", variable=self.var_policy, value="LRU", fg="#f8fafc", bg="#1e293b", selectcolor="#0f172a", font=("Helvetica", 10), command=self.on_policy_change).pack(side="left", padx=12, pady=12)
        tk.Radiobutton(f_controls, text="LFU (Deletes least popular)", variable=self.var_policy, value="LFU", fg="#f8fafc", bg="#1e293b", selectcolor="#0f172a", font=("Helvetica", 10), command=self.on_policy_change).pack(side="left", padx=8, pady=12)

        f_telemetry = tk.LabelFrame(self.tab_telemetry, text="  Real-Time Performance Monitor ", fg="#e2e8f0", bg="#1e293b", bd=1, font=("Helvetica", 11, "bold"))
        f_telemetry.pack(fill="both", expand=True, padx=15, pady=8)

        self.lbl_stats = tk.Label(f_telemetry, text="Hits: 0 | Misses: 0 | Ratio: 0.0%", fg="#38bdf8", bg="#1e293b", font=("Helvetica", 11, "bold"))
        self.lbl_stats.pack(anchor="w", padx=15, pady=12)

        self.lbl_latency = tk.Label(f_telemetry, text="Speed -> Average: 0.00ms | Slowest 1%: 0.00ms", fg="#4ade80", bg="#1e293b", font=("Helvetica", 11, "bold"))
        self.lbl_latency.pack(anchor="w", padx=15, pady=6)

        tk.Button(f_telemetry, text=" Simulate 10,000 Users Online", bg="#16a34a", fg="white", font=("Helvetica", 11, "bold"), padx=12, pady=6, command=self.run_traffic_simulation).pack(side="left", padx=15, pady=20)

    def build_cpp_tab(self):
        f_cpp = tk.LabelFrame(self.tab_cpp, text="  Native C++ Accelerator ", fg="#e2e8f0", bg="#1e293b", bd=1, font=("Helvetica", 11, "bold"))
        f_cpp.pack(fill="both", expand=True, padx=15, pady=15)

        tk.Label(f_cpp, text="Compiles and runs 'engine_perf.cpp' to benchmark system hardware capabilities.", fg="#94a3b8", bg="#1e293b", font=("Helvetica", 10, "italic")).pack(anchor="w", padx=12, pady=(6, 12))
        tk.Button(f_cpp, text="🔨 Build & Run Benchmark", bg="#7c3aed", fg="white", font=("Helvetica", 11, "bold"), padx=12, pady=6, command=self.run_cpp_binary).pack(anchor="w", padx=12, pady=8)

        self.cpp_output = tk.Text(f_cpp, bg="#0f172a", fg="#38bdf8", font=("Consolas", 11), height=14, width=75, bd=0, highlightthickness=0)
        self.cpp_output.pack(padx=12, pady=12, fill="both", expand=True)
        self.cpp_output.insert("1.0", "Ready. Click the button above to test your CPU's native processing speed.")

    def build_help_tab(self):
        instructions = f"""
Welcome to LuminousCart!

1. PRICING OPS: Look up items (ID 1 to 1000). Create instant flash sale overrides.
2. E-COMMERCE CART CHECKOUT: Test purchasing multiple products instantly with stock updates.
3. TELEMETRY: Monitor hits, misses, cache ratios, and simulated loads.
4. C++ ENGINE: Benchmark local hardware capabilities.
5. REST API ENDPOINT: 
    http://localhost:{self.port}/api/v1/checkout?cart=1:2,5:1
"""
        txt = tk.Text(self.tab_help, bg="#1e293b", fg="#e2e8f0", font=("Consolas", 11), wrap="word", bd=0, highlightthickness=0)
        txt.insert("1.0", instructions)
        txt.config(state="disabled")
        txt.pack(expand=True, fill="both", padx=20, pady=20)

    def run_cpp_binary(self):
        def worker():
            self.cpp_output.delete("1.0", tk.END)
            self.cpp_output.insert(tk.END, "[System] Looking for C++ Compiler (g++)...\n")

            exe_file = os.path.join(HERE, "engine_perf.exe" if os.name == "nt" else "engine_perf")
            compile_cmd = f'g++ -O3 -std=c++17 "{CPP_PATH}" -o "{exe_file}"'

            try:
                comp_res = subprocess.run(compile_cmd, capture_output=True, text=True, shell=True, timeout=10)
                if comp_res.returncode != 0:
                    self.cpp_output.insert(tk.END, f"\n[Setup Required] Could not compile.\nMake sure you have a C++ compiler installed.\n\nError Details:\n{comp_res.stderr}\n")
                    return
                self.cpp_output.insert(tk.END, "[System] Compilation successful! Running...\n\n")
            except Exception as e:
                self.cpp_output.insert(tk.END, f"[Execution Error]: {e}\n")
                return

            try:
                run_res = subprocess.run(f'"{exe_file}"', capture_output=True, text=True, shell=True, timeout=5)
                self.cpp_output.insert(tk.END, run_res.stdout)
            except Exception as e:
                self.cpp_output.insert(tk.END, f"[Execution Error]: {e}\n")

        threading.Thread(target=worker, daemon=True).start()

    def on_inspect(self):
        try:
            pid = int(self.entry_inspect_id.get())
            price, source, latency_ms = cache_engine.get_price(pid)
            if price < 0:
                self.lbl_inspect_res.config(text="Result: Not Found in Database", fg="#f87171")
            else:
                self.lbl_inspect_res.config(text=f"Result: ${price:.2f} [{source}] ({latency_ms}ms)", fg="#4ade80")
            self.update_telemetry()
        except ValueError:
            messagebox.showerror("Format Error", "Please enter a valid whole number for the Product ID.")

    def on_push_override(self):
        try:
            pid = int(self.entry_override_id.get())
            new_price = float(self.entry_override_price.get())
            persist = self.var_persist.get()
            cache_engine.set_price_override(pid, new_price, persist=persist)
            messagebox.showinfo("Success", f"Product {pid} instantly updated to ${new_price:.2f}")
            self.update_telemetry()
        except ValueError:
            messagebox.showerror("Format Error", "Please ensure ID is a whole number and Price is a decimal.")

    def on_clear_overrides(self):
        cache_engine.clear_overrides()
        messagebox.showinfo("Cleared", "All instant flash sales have been disabled.")
        self.update_telemetry()

    def on_gui_checkout(self):
        def worker():
            try:
                cart_str = self.entry_cart.get().strip()
                if not cart_str:
                    self.after(0, lambda: messagebox.showerror("Error", "Cart cannot be empty."))
                    return

                cart_items = {}
                for item in cart_str.split(","):
                    pid, qty = item.split(":")
                    cart_items[pid.strip()] = int(qty.strip())

                success, message, total = cache_engine.checkout_cart(cart_items)
                if success:
                    self.after(0, lambda: self.lbl_checkout_res.config(text=f"Success! Order Total: ${total:.2f}", fg="#4ade80"))
                    self.after(0, lambda: messagebox.showinfo("Order Placed", f"{message}\nTotal Billed: ${total:.2f}"))
                else:
                    self.after(0, lambda: self.lbl_checkout_res.config(text=f"Failed: {message}", fg="#f87171"))
                    self.after(0, lambda: messagebox.showwarning("Checkout Failed", message))
                self.after(0, self.update_telemetry)
            except Exception as e:
                self.after(0, lambda: messagebox.showerror("Format Error", f"Use correct cart format like: 1:2,5:1\nError: {e}"))
        
        threading.Thread(target=worker, daemon=True).start()

    def on_policy_change(self):
        cache_engine.set_policy(self.var_policy.get())
        self.update_telemetry()

    def update_telemetry(self):
        s = cache_engine.stats()
        self.lbl_stats.config(text=f"Hits: {s['hits']:,} | Misses: {s['misses']:,} | Success Ratio: {s['hit_ratio']}%")
        self.lbl_latency.config(text=f"Speed -> Avg (p50): {s['p50_ms']}ms | Slowest 1% (p99): {s['p99_ms']}ms")

    def run_traffic_simulation(self):
        def worker():
            random.seed(42)
            hot_keys, cold_keys = list(range(1, 201)), list(range(201, 1001))
            start = time.perf_counter()
            for _ in range(10000):
                pid = random.choice(hot_keys) if random.random() < 0.8 else random.choice(cold_keys)
                cache_engine.get_price(pid)
            elapsed = time.perf_counter() - start
            self.after(0, lambda: messagebox.showinfo("Simulation Complete", f"Processed 10,000 queries in {elapsed:.3f} seconds!"))
            self.after(0, self.update_telemetry)
        threading.Thread(target=worker, daemon=True).start()


def main():
    global cache_engine
    ensure_environment()
    cache_engine = ProductionCacheEngine(DB_PATH, capacity=200, ttl_seconds=30.0)
    
    port = find_available_port(8000)
    threading.Thread(target=start_web_server, args=(port,), daemon=True).start()

    app = FlashSaleManagerApp(port)
    app.mainloop()

if __name__ == "__main__":
    main()