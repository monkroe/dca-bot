#!/bin/bash
set -e

echo "[check] Python syntax..."
python3 -m py_compile src/kraken_run.py && echo "[ok] kraken_run.py" || echo "[fail] kraken_run.py"
python3 -m py_compile src/strike_run.py && echo "[ok] strike_run.py" || echo "[fail] strike_run.py"
python3 -m py_compile src/ohlc.py && echo "[ok] ohlc.py" || echo "[fail] ohlc.py"
