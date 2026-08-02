"""Crypto.com Exchange v1 signature payload builder.

THE ONLY THING WORTH TESTING IN THE PROBE. Everything else it does is one HTTP
call; this is the part that is wrong silently. A bad signature comes back as
`40101` with no hint about WHICH rule was broken -- sorting, nesting, null
handling or the payload order -- so the rule is pinned here against Crypto.com's
own published sample rather than against my reading of it.

Vector reproduced verbatim from the official Python example, quoted in
`robert-os-hub/docs/05-roadmap/cdc-exchange-api-v1-research.md`.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
os.environ.setdefault("CDC_API_KEY", "API_KEY")
os.environ.setdefault("CDC_API_SECRET", "SECRET_KEY")

from _harness import Runner  # noqa: E402
import cdc_probe as cdc  # noqa: E402

# The docs' own example: nested list of order dicts inside `params`.
OFFICIAL_PARAMS = {
    "contingency_type": "LIST",
    "order_list": [
        {"instrument_name": "ONE_USDT", "side": "BUY", "type": "LIMIT",
         "price": "0.24", "quantity": "1.0"},
        {"instrument_name": "ONE_USDT", "side": "BUY", "type": "STOP_LIMIT",
         "price": "0.27", "quantity": "1.0", "trigger_price": "0.26"},
    ],
}

OFFICIAL_PARAM_STR = (
    "contingency_typeLIST"
    "order_listinstrument_nameONE_USDTprice0.24quantity1.0sideBUYtypeLIMIT"
    "instrument_nameONE_USDTprice0.27quantity1.0sideBUYtrigger_price0.26typeSTOP_LIMIT"
)


def test_official_vector(t):
    t.check("official nested sample", cdc.params_to_str(OFFICIAL_PARAMS, 0),
            OFFICIAL_PARAM_STR)


def test_keys_are_sorted_at_every_level(t):
    # Insertion order must not survive. Python dicts preserve it, so a builder
    # that forgets to sort passes every hand-written example and fails in
    # production the first time a caller happens to build params in a different
    # order.
    t.check("top level", cdc.params_to_str({"b": "2", "a": "1"}, 0), "a1b2")
    t.check("inside a list element",
            cdc.params_to_str({"k": [{"z": "1", "a": "2"}]}, 0), "ka2z1")


def test_none_is_the_four_characters_null(t):
    # Not "None", not an empty string. Python's str(None) would give "None" and
    # the signature would fail with no explanation of why.
    t.check("null", cdc.params_to_str({"a": None}, 0), "anull")


def test_list_elements_do_not_re_emit_the_key(t):
    # The key appears once, then each element contributes its own pairs. Emitting
    # the key per element is the intuitive implementation and the wrong one.
    t.check("key emitted once",
            cdc.params_to_str({"xs": [{"a": "1"}, {"a": "2"}]}, 0), "xsa1a2")


def test_recursion_stops_at_max_level(t):
    # MAX_LEVEL is part of the contract -- the same constant appears in the
    # official Python and Java samples -- not a tuning knob.
    t.check("constant matches the published samples", cdc.MAX_LEVEL, 3)
    deep = {"a": [{"b": [{"c": [{"d": "1"}]}]}]}
    t.check_true("deep input does not raise", isinstance(cdc.params_to_str(deep, 0), str))


def test_empty_params(t):
    t.check("no params", cdc.params_to_str({}, 0), "")


def test_payload_order_is_method_id_key_params_nonce(t):
    # Step 3 of the published algorithm. Reordering these is the other silent
    # way to get 40101, and it cannot be caught by looking at param_str alone.
    body = {"method": "private/get-trades", "id": 7, "api_key": "K",
            "params": {"b": "2", "a": "1"}, "nonce": 1785600000000}
    payload = (body["method"] + str(body["id"]) + body["api_key"]
               + cdc.params_to_str(body["params"], 0) + str(body["nonce"]))
    t.check("payload", payload, "private/get-trades7Ka1b21785600000000")


if __name__ == "__main__":
    sys.exit(Runner("cdc signature payload").run([
        ("official vector", test_official_vector),
        ("keys sorted at every level", test_keys_are_sorted_at_every_level),
        ("None is the four characters null", test_none_is_the_four_characters_null),
        ("list elements do not re-emit the key", test_list_elements_do_not_re_emit_the_key),
        ("recursion stops at MAX_LEVEL", test_recursion_stops_at_max_level),
        ("empty params", test_empty_params),
        ("payload order", test_payload_order_is_method_id_key_params_nonce),
    ]))
