# LuminousCart
A rapid e-commerce platform with built-in caching, database concurrency, rate limiting, C++ performance tools, a Python desktop dashboard, and a live web monitor.



## Features

* **Dual-Policy Caching**: Switch between LRU and LFU cache eviction strategies.
* **Database Concurrency**: SQLite with Write-Ahead Logging (WAL) mode enabled for high-throughput reads and writes.
* **Rate Limiting**: Built-in token bucket rate limiting to control API request traffic.
* **C++ Performance Accelerator**: Native C++ background tool using ring buffers for hardware benchmarking.
* **Dual Interfaces**: 
  * Python Tkinter desktop GUI for system monitoring and flash sale controls.

---

## Tech Stack

* **Backend / Desktop GUI**: Python 3.8+ (Tkinter, Standard Library HTTP server)
* **Performance Tool**: C++
* **Database**: SQLite3 (WAL mode)
* **Frontend**: HTML5, CSS3, Vanilla JavaScript

---


### Prerequisites
* **Python 3.8+** installed on your system.
* **C++ Compiler** (optional, for compiling the C++ performance component: `g++` or `clang++`).

### Step-by-Step Instructions

1. **Clone the repository**:
   ```bash
   git clone [https://github.com/harsmatharoo/luminous-cart.git](https://github.com/harsmatharoo/luminous-cart.git)
   cd luminous-cart

2. **Run the Application**:
    python app.py

   Open index.html directly in any modern web browser (e.g., double-clicking the file or dragging it into Chrome/Firefox/Edge) to view the live client dashboard and telemetry polling.
