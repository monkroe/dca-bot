#!/bin/bash
echo "🔍 Checking Python syntax..."
python3 -m py_compile src/kraken_run.py && echo "✅ kraken_run.py OK" || echo "❌ kraken_run.py FAIL"
python3 -m py_compile src/strike_run.py && echo "✅ strike_run.py OK" || echo "❌ strike_run.py FAIL"
