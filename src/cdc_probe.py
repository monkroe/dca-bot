#!/usr/bin/env python3
"""Crypto.com Exchange v1 -- signature probe. ONE question, answered live.

WHY A PROBE AND NOT THE SYNC. The signature is the whole risk in this
integration: everything after it is pagination and upserts, which this repo has
already done twice. So the first thing built is the smallest thing that can fail
for the right reason -- sign one request, send it, print what came back.

WHY YOU RUN IT AND NOT A WORKFLOW. The key stays in YOUR shell. It is not in the
repo, not in GitHub secrets, and not in any file. If the signature turns out to
be wrong, nothing has been stored anywhere that would need cleaning up.

    export CDC_API_KEY='...'
    export CDC_API_SECRET='...'
    python3 src/cdc_probe.py

READ ONLY. Calls `private/get-trades`, `private/get-deposit-history` and
`private/get-withdrawal-history`. It cannot place, amend, cancel or withdraw --
those methods are not in this file.

WHAT IT ANSWERS, in order:
  1. Does the signature verify at all?           -> anything other than 40101
  2. Which read permissions does the key carry?  -> per-endpoint result
  3. Is `trade_id` unique per ACCOUNT or only per instrument?
     The docs do not say (recorded as NERASTA in the research), and the whole
     dedupe key depends on it. With real trades across two instruments this is
     answerable by looking.

Source for the signature rules, quoted verbatim in the research doc:
`robert-os-hub/docs/05-roadmap/cdc-exchange-api-v1-research.md`.
Docs moved host in 2026: exchange-developer.crypto.com, NOT exchange-docs.
"""

import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = "https://api.crypto.com/exchange/v1"

API_KEY = os.environ.get("CDC_API_KEY", "")
API_SECRET = os.environ.get("CDC_API_SECRET", "")

# The official sample caps recursion here. Reproduced rather than chosen: the
# same constant appears in Crypto.com's Python and Java samples, so it is part
# of the contract and not a tuning knob.
MAX_LEVEL = 3


def params_to_str(obj, level: int) -> str:
    """Verbatim port of the official sample. Do not 'improve' it.

    Sort keys at every level, concatenate key+value with no separators, recurse
    into lists WITHOUT re-emitting the key, and serialise None as the four
    characters `null`. Any deviation produces a signature that is wrong in a way
    the error message will not explain.
    """
    if level >= MAX_LEVEL:
        return str(obj)
    out = ""
    for key in sorted(obj):
        out += key
        value = obj[key]
        if value is None:
            out += "null"
        elif isinstance(value, list):
            for sub in value:
                out += params_to_str(sub, level + 1)
        else:
            out += str(value)
    return out


def call(method: str, params: dict, req_id: int):
    """Sign and POST one request. Returns (http_status, parsed_body)."""
    body = {
        "id": req_id,
        "method": method,
        "api_key": API_KEY,
        "params": params,
        "nonce": int(time.time() * 1000),
    }
    payload = (body["method"] + str(body["id"]) + body["api_key"]
               + params_to_str(body["params"], 0) + str(body["nonce"]))
    body["sig"] = hmac.new(API_SECRET.encode(), payload.encode(),
                           hashlib.sha256).hexdigest()

    req = urllib.request.Request(
        f"{BASE}/{method}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"raw": raw[:400]}


def describe(status, data) -> str:
    code = (data or {}).get("code")
    if code == 0:
        return "OK"
    # 40101 is the one that means the signature itself did not verify. Every
    # other code means the request WAS accepted and something else was refused,
    # which is a different and much better problem.
    known = {
        40101: "SIGNATURE FAILED -- the payload or the secret is wrong",
        40102: "nonce rejected (clock skew?)",
        40103: "IP not whitelisted",
        40104: "method not permitted for this key -- missing permission",
        40401: "method not found",
        42901: "rate limited",
    }
    return f"code={code} http={status} {known.get(code, (data or {}).get('message', ''))}"


def main() -> int:
    if not API_KEY or not API_SECRET:
        print("Set CDC_API_KEY and CDC_API_SECRET in your shell first.")
        print("Do NOT put them in a file, the repo, or a chat message.")
        return 2

    # Deliberately tiny: one page, smallest useful window. A probe that pulls a
    # year of history to prove a signature works is a probe that also has to be
    # right about pagination.
    checks = [
        ("private/get-trades", {"limit": 20}),
        ("private/get-deposit-history", {"page_size": 5}),
        ("private/get-withdrawal-history", {"page_size": 5}),
    ]

    print(f"Crypto.com Exchange v1 probe -- {BASE}\n")
    results = {}
    for i, (method, params) in enumerate(checks, start=1):
        status, data = call(method, params, i)
        verdict = describe(status, data)
        print(f"  {method:34s} {verdict}")
        results[method] = data
        # 1 req/s on get-trades, per the docs. Spaced rather than fired.
        time.sleep(1.2)

    trades = (((results.get("private/get-trades") or {}).get("result") or {})
              .get("data") or [])
    print(f"\n  trades returned: {len(trades)}")

    if trades:
        # The NERASTA question. If one trade_id shows up under two different
        # instruments, the id is per-instrument and a composite dedupe key is
        # mandatory. Silence here is not proof of uniqueness -- it only means
        # this sample did not disprove it.
        seen = {}
        collisions = []
        for t in trades:
            tid, inst = t.get("trade_id"), t.get("instrument_name")
            if tid in seen and seen[tid] != inst:
                collisions.append((tid, seen[tid], inst))
            seen[tid] = inst
        instruments = sorted({t.get("instrument_name") for t in trades})
        print(f"  instruments in sample: {', '.join(instruments)}")
        print(f"  distinct trade_id: {len(seen)} of {len(trades)}")
        if collisions:
            print("  trade_id COLLIDES across instruments -> composite key REQUIRED")
        elif len(instruments) > 1:
            print("  no collision across >1 instrument -- consistent with account-wide,"
                  " NOT proof of it")
        else:
            print("  only one instrument in sample -- says nothing about scope")

        first = trades[0]
        print("\n  fields on a trade row:")
        print("   ", ", ".join(sorted(first)))

    for name in ("private/get-deposit-history", "private/get-withdrawal-history"):
        rows = (((results.get(name) or {}).get("result") or {})
                .get("deposit_list") or ((results.get(name) or {}).get("result") or {})
                .get("withdrawal_list") or [])
        if rows:
            # The load-bearing field: the network fee is invisible to the
            # receiving exchange, so if it is absent here it is absent
            # everywhere.
            print(f"\n  {name} first row fields:")
            print("   ", ", ".join(sorted(rows[0])))
            print(f"    fee present: {'fee' in rows[0]}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
