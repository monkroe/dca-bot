#!/usr/bin/env python3
"""What gets written to `raw` when Kraken refuses an order.

Failures used to record `{"error": ...}` and nothing else, so the file said
what Kraken thought of a request nobody kept. On 2026-07-30 an
EOrder:Insufficient funds sat there with no volume, no price and no balance
beside it, and every question worth asking had to be reconstructed from an
Actions log that rotates.

Two properties matter and only one is obvious:
  * the request is present, so the failure can be read a week later;
  * no credential is, because this row is readable by anything that can read
    the table.
"""
import json

from _harness import kr, Runner

PARAMS = {
    "pair": "KASUSD",
    "type": "buy",
    "ordertype": "limit",
    "price": "0.027770",
    "volume": "360.00000000",
    "oflags": "post,fciq",
    "cl_ordid": "dca-KASUSD-2026-07-30-0700",
    "nonce": "1753876543210",
}
ERR = kr.KrakenError(["EOrder:Insufficient funds"])


def t_request_is_kept(r):
    raw = json.loads(kr._failure_raw(PARAMS, ERR))
    r.check("volume kept", raw["request"]["volume"], "360.00000000")
    r.check("price kept", raw["request"]["price"], "0.027770")
    r.check("pair kept", raw["request"]["pair"], "KASUSD")
    r.check("client id kept", raw["request"]["cl_ordid"], "dca-KASUSD-2026-07-30-0700")
    r.check_true("error kept", "Insufficient funds" in raw["error"])
    r.check_true("timestamped", raw["at"].startswith("20"))


def t_no_nonce(r):
    # The nonce is per-key and strictly increasing; it belongs to the signing
    # layer and never in a stored record.
    raw = json.loads(kr._failure_raw(PARAMS, ERR))
    r.check("nonce dropped", "nonce" in raw["request"], False)


def t_no_credentials_anywhere(r):
    # The blunt version of the same check: assert on the serialised string, so
    # a key smuggled in through **context is caught too.
    blob = kr._failure_raw(PARAMS, ERR, spendable_usd=34.94, held_usd=20.0,
                           balance_source="BalanceEx")
    for secret in (kr.KRAKEN_API_KEY, kr.KRAKEN_API_SECRET, kr.SUPABASE_KEY):
        r.check(f"{secret[:6]}... absent", secret in blob, False)
    for name in ("API-Key", "API-Sign", "apikey", "Authorization"):
        r.check(f"{name} absent", name in blob, False)


def t_context_merges(r):
    raw = json.loads(kr._failure_raw(PARAMS, ERR, spendable_usd=34.94,
                                     held_usd=20.0, balance_source="BalanceEx"))
    # THE 07-30 question: how much could we actually spend at that moment.
    r.check("spendable recorded", raw["spendable_usd"], 34.94)
    r.check("held recorded", raw["held_usd"], 20.0)
    r.check("source recorded", raw["balance_source"], "BalanceEx")
    r.check("request still there", raw["request"]["pair"], "KASUSD")


def t_degenerate_inputs(r):
    # A failure record must never be the thing that raises during a failure.
    r.check("empty params", json.loads(kr._failure_raw({}, ERR))["request"], {})
    r.check("None params", json.loads(kr._failure_raw(None, ERR))["request"], {})
    r.check_true("None error", "None" in json.loads(kr._failure_raw(PARAMS, None))["error"])


def t_note_is_the_same_record(r):
    # _failure_note exists for the re-peg path, whose raw must be MERGED --
    # replacing it there resets repeg_count and the leg re-pegs forever.
    note = kr._failure_note(PARAMS, ERR, leg="repeg")
    r.check_true("a dict, not a string", isinstance(note, dict))
    r.check("same request", note["request"], json.loads(kr._failure_raw(PARAMS, ERR))["request"])
    r.check("leg tagged", note["leg"], "repeg")
    # It must survive being nested inside an existing raw and re-serialised.
    merged = json.loads(json.dumps({"repeg_count": 2, "last_failure": note}))
    r.check("count survives the merge", merged["repeg_count"], 2)
    r.check("failure survives the merge", merged["last_failure"]["request"]["pair"], "KASUSD")


OPEN_ORDERS = {
    "open": {
        "OQCLML-BW3P3-BUCMWZ": {
            "cl_ord_id": "dca-KASUSD-2026-07-29-0700",
            "status": "open",
            "descr": {"pair": "KASUSD", "type": "buy", "ordertype": "limit",
                      "price": "0.027000", "order": "buy 740 KASUSD @ limit 0.027"},
            "vol": "740.00000000",
            "vol_exec": "0.00000000",
            # Average FILL price, zero on an untouched order. Reading this
            # instead of descr.price values every resting order at nothing --
            # which is the whole answer we came for.
            "price": "0.00000",
            "cost": "0.00000",
            "oflags": "post,fciq",
        },
        "OPART-1": {
            # Half filled. What is still LOCKED is the unfilled half, so the
            # figure has to be (vol - vol_exec) * price and not vol * price --
            # the second one overstates the shortfall by whatever already
            # bought, which is exactly the number you would act on.
            "descr": {"pair": "KASUSD", "type": "buy", "ordertype": "limit",
                      "price": "0.030000"},
            "vol": "1000.0", "vol_exec": "400.0", "price": "0.030000",
        },
        "OSELL-1": {
            "descr": {"pair": "XBTUSD", "type": "sell", "ordertype": "limit",
                      "price": "70000.0"},
            "vol": "0.5", "vol_exec": "0.1", "price": "0.0",
        },
    }
}


def _with_stubbed_kraken(fn):
    real = kr.kraken_private
    kr.kraken_private = fn
    try:
        return kr._open_orders_digest()
    finally:
        kr.kraken_private = real


def t_digest_values_the_limit_price(r):
    d = _with_stubbed_kraken(lambda ep, params=None: OPEN_ORDERS)
    buy = [o for o in d if o["txid"] == "OQCLML-BW3P3-BUCMWZ"][0]
    r.check("limit price, not average fill", buy["price"], 0.027)
    r.check("locked quote is not zero", round(buy["quote_locked"], 2), 19.98)
    r.check("client id carried", buy["cl_ord_id"], "dca-KASUSD-2026-07-29-0700")
    r.check("pair carried", buy["pair"], "KASUSD")


def t_digest_partial_fill(r):
    d = _with_stubbed_kraken(lambda ep, params=None: OPEN_ORDERS)
    part = [o for o in d if o["txid"] == "OPART-1"][0]
    r.check("only the unfilled half is locked", part["quote_locked"], 18.0)
    sell = [o for o in d if o["txid"] == "OSELL-1"][0]
    # Only a BUY locks quote currency; a sell locks the base asset, and
    # reporting a number there would overstate what the USD shortfall was.
    r.check("sell locks no quote", sell["quote_locked"], None)
    r.check("executed volume kept", sell["vol_exec"], 0.1)


def t_digest_never_raises(r):
    def boom(ep, params=None):
        raise kr.KrakenError(["EGeneral:Permission denied"])
    d = _with_stubbed_kraken(boom)
    r.check_true("error reported, not raised", "Permission denied" in d["error"])
    r.check("empty book", _with_stubbed_kraken(lambda ep, params=None: {"open": {}}), [])
    r.check("missing key", _with_stubbed_kraken(lambda ep, params=None: {}), [])


TESTS = [
    ("digest values the limit price", t_digest_values_the_limit_price),
    ("digest handles a partial fill", t_digest_partial_fill),
    ("digest never raises", t_digest_never_raises),
    ("the request is kept", t_request_is_kept),
    ("nonce is dropped", t_no_nonce),
    ("no credentials anywhere", t_no_credentials_anywhere),
    ("context merges in", t_context_merges),
    ("degenerate inputs", t_degenerate_inputs),
    ("note matches raw", t_note_is_the_same_record),
]

if __name__ == "__main__":
    raise SystemExit(Runner("_failure_raw").run(TESTS))
