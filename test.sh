#!/bin/bash

CHECK=$'\U0001F50D'  # 🔍
OK=$'\U00002705'     # ✅
FAIL=$'\U0000274C'   # ❌

echo "${CHECK} Checking Python syntax..."

failed=0

python3 -m py_compile src/kraken_run.py && echo "${OK} kraken_run.py OK" || { echo "${FAIL} kraken_run.py FAIL"; failed=1; }
python3 -m py_compile src/strike_run.py && echo "${OK} strike_run.py OK" || { echo "${FAIL} strike_run.py FAIL"; failed=1; }
python3 -m py_compile src/ohlc.py       && echo "${OK} ohlc.py OK"       || { echo "${FAIL} ohlc.py FAIL"; failed=1; }
python3 -m py_compile src/kraken_sync.py && echo "${OK} kraken_sync.py OK" || { echo "${FAIL} kraken_sync.py FAIL"; failed=1; }

exit $failed
