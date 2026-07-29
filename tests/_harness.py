"""Import kraken_run without credentials, and a two-line assertion runner.

kraken_run reads KRAKEN_API_KEY, KRAKEN_API_SECRET, SUPABASE_URL and
SUPABASE_SERVICE_ROLE_KEY with os.environ[...] at MODULE level, so importing it
raises KeyError unless those names exist. That makes the module untestable as
shipped; stubbing the environment here is the smallest fix that does not touch
the live buy path. Recorded as a known testability defect rather than hidden:
the right long-term shape is lazy credential resolution inside the request
signer, so importing the module proves nothing about the environment.

The stub values are never used. Every function under test here is pure and
performs no I/O -- that is the whole reason these two were extracted.
"""
import os
import sys
import traceback
from pathlib import Path

for _name in ("KRAKEN_API_KEY", "KRAKEN_API_SECRET",
              "SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY"):
    os.environ.setdefault(_name, "test-stub-not-used")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import kraken_run as kr  # noqa: E402


class Runner:
    """Minimal assertion runner: no pytest, so this runs anywhere test.sh runs."""

    def __init__(self, title: str):
        self.title = title
        self.passed = 0
        self.failed = 0

    def check(self, name: str, actual, expected):
        if actual == expected:
            self.passed += 1
            return
        self.failed += 1
        print(f"  FAIL  {name}")
        print(f"        expected: {expected!r}")
        print(f"        actual:   {actual!r}")

    def check_true(self, name: str, value):
        self.check(name, bool(value), True)

    def run(self, tests):
        print(f"{self.title} ({len(tests)} branches)")
        for name, fn in tests:
            try:
                fn(self)
            except Exception:
                self.failed += 1
                print(f"  ERROR {name}")
                traceback.print_exc()
        total = self.passed + self.failed
        status = "OK" if self.failed == 0 else "FAILED"
        print(f"  {status}: {self.passed}/{total} assertions")
        return 0 if self.failed == 0 else 1
