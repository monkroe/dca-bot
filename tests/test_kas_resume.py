"""Explicit same-day KAS resume tests; all providers are replaced in memory."""
from datetime import datetime, timedelta, timezone
from _rob21_world import World, MAKER_SETTINGS, ORDER, kr

NOW = datetime.now(kr.CHICAGO_TZ)
TODAY = NOW.strftime("%Y-%m-%d")
ORDER = {**ORDER, "target_time": "07:25", "enabled": True}
CL = f"dca-KASUSD-{TODAY}-0700"
NEW_CL = f"dca-KASUSD-{TODAY}-0725-resume"
REQUEST = kr.KasResume(TODAY, "07:25", 9)


def world(**updates):
    w = World()
    w.balance = (100.0, 0.0, "BalanceEx")
    w.add_row(**{
        "id": 9, "dca_order_id": 1, "parent_event_id": "event-original",
        "trade_date_chicago": TODAY, "attempt_type": "maker_limit",
        "status": "skipped_insufficient_funds", "reason": "low spendable",
        "requested_quote_amount_base": 10.0, "execution_finished_at": "finished",
        "cl_ord_id": CL,
        "raw": {"at": "original", "spendable_usd": 3.0, "needed_usd": 10.0,
                "kraken_cl": kr.provider_client_id("maker_limit", CL, 0)},
        **updates,
    })
    return w


def run(w, request=REQUEST, order=ORDER, settings=MAKER_SETTINGS):
    with w.installed():
        return kr.execute_pair(order, settings, TODAY, "test-user", kas_resume=request)


good = world()
original = dict(good.rows[0])
assert run(good)["status"] == "limit_open"
assert len(good.rows) == 1
row = good.row(NEW_CL)
assert row["parent_event_id"] == "event-original"
assert row["execution_finished_at"] is None
assert good.raw(NEW_CL)["kas_resume"]["original_execution"] == original
params, = good.calls("AddOrder")
assert params["ordertype"] == "limit" and params["oflags"] == "post,fciq"
assert params["cl_ord_id"] == good.raw(NEW_CL)["kraken_cl"]
assert good.events.index(("kraken", "AddOrder")) > 0
assert run(good)["status"] == "resume_not_claimed"
assert len(good.calls("AddOrder")) == 1

for changes in [{"status": "claimed"}, {"status": "filled"}, {"status": "failed_kraken"},
                {"status": "skipped_above_cap"}, {"order_id": "provider-order"},
                {"filled_base_volume": 1}, {"dca_order_id": 42},
                {"requested_quote_amount_base": 20.0}, {"raw": {"needed_usd": 10}}]:
    w = world(**changes)
    assert run(w)["status"] == "resume_not_claimed", changes
    assert not w.calls("AddOrder"), changes

for request in (REQUEST._replace(execution_id=10), REQUEST._replace(target_time="07:24"),
                REQUEST._replace(trade_date="2026-01-01")):
    assert run(world(), request=request)["status"] == "resume_not_claimed"

w = world()
w.add_row(id=10, trade_date_chicago=TODAY, attempt_type="market", status="filled",
          cl_ord_id="older-force", order_id="matching-order")
assert run(w)["status"] == "resume_not_claimed" and not w.calls("AddOrder")

w = world()
w.clock_window = (NOW - timedelta(hours=2), NOW - timedelta(hours=1))
assert run(w)["status"] == "resume_not_claimed" and not w.calls("AddOrder")

w = world()
w.sb_update = lambda *_a, **_k: []  # another invocation won the CAS
assert run(w)["status"] == "resume_not_claimed" and not w.calls("AddOrder")

w = world()
w.balance = (0.50, 4.0, "BalanceEx")
assert run(w)["status"] == "skipped_insufficient_funds"
assert run(w)["status"] == "resume_not_claimed" and not w.calls("AddOrder")
assert w.raw(NEW_CL)["kas_resume"]["original_execution"]["status"] == "skipped_insufficient_funds"

w = world()
w.balance = (100.0, 0.0, "Balance")
assert run(w)["status"] == "manual_required" and not w.calls("AddOrder")

w = world()
with w.installed():
    kr.get_cap_context = lambda *_a, **_k: (0.01, None, "H7")
    result = kr.execute_pair(ORDER, {**MAKER_SETTINGS, "cap_require_above_h90": False},
                             TODAY, "test-user", kas_resume=REQUEST)
assert result["status"] == "skipped_above_cap" and not w.calls("AddOrder")
assert w.raw(NEW_CL)["kas_resume"]["original_execution"]["reason"] == "low spendable"

# Resume keeps an immutable maker window even if the ordinary schedule moves.
recorded_end = datetime.fromisoformat(good.raw(NEW_CL)["kas_resume"]["window_end"])
assert kr._limit_window_end(good.row(NEW_CL), {"target_time": "21:00"}) == recorded_end

for ack in ({}, {"txid": []}, {"txid": ["first", "second"]},
            {"txid": [None]}, {"txid": [""]}):
    w = world()
    w.add_order = lambda _params: ack
    assert run(w)["submission_state"] == "UNKNOWN"
    assert w.row(NEW_CL)["status"] == "claimed" and len(w.calls("AddOrder")) == 1
    assert run(w)["status"] == "resume_not_claimed" and len(w.calls("AddOrder")) == 1

w = world()
real_update = w.sb_update
w.sb_update = lambda table, filters, updates: ([] if updates.get("status") == "limit_open"
                                              else real_update(table, filters, updates))
assert run(w)["submission_state"] == "UNKNOWN"
assert w.row(NEW_CL)["status"] == "claimed" and len(w.calls("AddOrder")) == 1

w = world()
w.endpoint_errors["AddOrder"] = TimeoutError("ambiguous")
assert run(w)["submission_state"] == "UNKNOWN"
assert w.row(NEW_CL)["status"] == "claimed" and len(w.calls("AddOrder")) == 1

w = world()
def postonly_then_fallback(params):
    if params["ordertype"] == "limit":
        raise kr.KrakenError(["EOrder:Post only order"])
    return {"txid": ["FALLBACK"]}
w.add_order = postonly_then_fallback
assert run(w)["status"] == "rejected_postonly"
limit, fallback = w.calls("AddOrder")
assert limit["ordertype"] == "limit" and fallback["ordertype"] == "market"
assert w.row(NEW_CL + "-fb")["parent_event_id"] == w.row(NEW_CL)["parent_event_id"]
assert "kas_resume" in w.raw(NEW_CL)

print("Explicit KAS resume mock tests passed")
