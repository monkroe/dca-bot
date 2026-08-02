#!/bin/bash

CHECK=$'\U0001F50D'  # 🔍
OK=$'\U00002705'     # ✅
FAIL=$'\U0000274C'   # ❌

# On 2026-07-30 two source mutations came back GREEN while the live code was in
# fact broken: a stale __pycache__ held the previous bytecode and the import
# never saw the change. This script happens to be safe from that -- py_compile
# below rewrites the cache from current source before anything imports it --
# but nothing said so, and the ad-hoc mutation runs that skip this step were
# not safe at all. Belt as well as braces, and the reason is written down:
# a gate that can report a pass for code it did not run is worse than no gate.
export PYTHONDONTWRITEBYTECODE=1

echo "${CHECK} Checking Python syntax..."

failed=0

python3 -m py_compile src/kraken_run.py && echo "${OK} kraken_run.py OK" || { echo "${FAIL} kraken_run.py FAIL"; failed=1; }
python3 -m py_compile src/strike_run.py && echo "${OK} strike_run.py OK" || { echo "${FAIL} strike_run.py FAIL"; failed=1; }
python3 -m py_compile src/ohlc.py       && echo "${OK} ohlc.py OK"       || { echo "${FAIL} ohlc.py FAIL"; failed=1; }
python3 -m py_compile src/kraken_sync.py && echo "${OK} kraken_sync.py OK" || { echo "${FAIL} kraken_sync.py FAIL"; failed=1; }
python3 -m py_compile src/cdc_probe.py   && echo "${OK} cdc_probe.py OK"   || { echo "${FAIL} cdc_probe.py FAIL"; failed=1; }

echo
echo "${CHECK} Schema check (every written column exists)..."
python3 scripts/check_schema_columns.py && echo "${OK} schema OK" || { echo "${FAIL} schema FAIL"; failed=1; }

echo
echo "${CHECK} Running unit tests (pure decision functions, no I/O)..."

for t in tests/test_*.py; do
  (cd tests && python3 "../$t") && echo "${OK} $(basename "$t") OK" || { echo "${FAIL} $(basename "$t") FAIL"; failed=1; }
done

exit $failed
