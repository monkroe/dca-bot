#!/bin/bash
echo "🔍 Checking Python syntax..."
python3 -m py_compile src/dca_run.py && echo "✅ dca_run.py OK" || echo "❌ dca_run.py FAIL"
python3 -m py_compile src/strike_run.py && echo "✅ strike_run.py OK" || echo "❌ strike_run.py FAIL"
