"""Strike's Telegram escaping: can an exception's text become markup?

WHY THIS EXISTS. `strike_run.tg_send` sent every message with
`parse_mode: HTML` and escaped nothing, while the failure and reconciliation
messages interpolate an exception whose text comes from Strike or from Python.
One `<` in it makes Telegram reject the whole message with HTTP 400, and
`tg_send` swallows the error -- so the alert saying a trade could not be closed
out would disappear with no trace. A rendered tag is the milder half; the
vanished alert is the reason this is tested.

The order is the whole property: escape first, THEN turn our own sentinels into
tags. Reverse it and content escaped afterwards eats the tags we just made,
which is the classic way an escaper is written so it does nothing.
"""
import os
import sys

from _harness import Runner  # sets sys.path to src/ and stubs Supabase creds

os.environ.setdefault("STRIKE_API_KEY", "test-stub-not-used")

import strike_run as sr  # noqa: E402

# The shape that actually reaches these messages: an HTTP error body from
# Strike, pasted whole into `Cannot finalize: {e}`.
HOSTILE = 'HTTP 400: {"error": "<b>bad</b> & <script>x</script>"}'


def test_markup_from_an_exception_is_shown_not_rendered(t):
    out = sr._tg_html(f"Cannot finalize: {HOSTILE}")
    t.check("no live bold", "<b>bad</b>" in out, False)
    t.check("no live script", "<script>" in out, False)
    t.check("shown escaped", "&lt;b&gt;bad&lt;/b&gt;" in out, True)


def test_ampersand_escaped_before_the_angle_brackets(t):
    # `&` must go first, or "&lt;" produced from "<" gets re-escaped into
    # "&amp;lt;" and the user reads the escape sequence instead of the text.
    t.check("single pass", sr._tg_html("a < b & c"), "a &lt; b &amp; c")


def test_our_own_titles_still_render(t):
    # Escaping is worthless if it also disarms the formatting we intend.
    out = sr._tg_html(sr.msg_fail("STRIKE DCA FAIL", "2026-08-04 | KASUSD"))
    t.check("bold opens", "<b>STRIKE DCA FAIL</b>" in out, True)
    t.check("no sentinel leaks", "\x00" in out, False)


def test_a_title_cannot_smuggle_a_tag(t):
    # The sentinels mark ORIGIN, not spelling: text that merely looks like a
    # tag is still content, wherever it came from.
    out = sr._tg_html(sr.msg_warn("<i>fake</i>", "body"))
    t.check("title escaped", "<i>fake</i>" in out, False)
    t.check("title shown", "&lt;i&gt;fake&lt;/i&gt;" in out, True)


def test_every_formatter_is_wired_to_the_sentinels(t):
    # Enumerated rather than sampled. A formatter left with a literal <b> would
    # keep working in the happy case and reintroduce the bug in its own message.
    for name in ("msg_ok", "msg_warn", "msg_fail", "msg_recon", "msg_dryrun"):
        raw = getattr(sr, name)("T", "b")
        t.check(f"{name} emits sentinel", sr.B_ON in raw, True)
        t.check(f"{name} has no literal tag", "<b>" in raw, False)


if __name__ == "__main__":
    sys.exit(Runner("strike telegram HTML escaping").run([
        ("markup from an exception is shown, not rendered",
         test_markup_from_an_exception_is_shown_not_rendered),
        ("ampersand escaped before angle brackets",
         test_ampersand_escaped_before_the_angle_brackets),
        ("our own titles still render", test_our_own_titles_still_render),
        ("a title cannot smuggle a tag", test_a_title_cannot_smuggle_a_tag),
        ("every formatter wired to the sentinels",
         test_every_formatter_is_wired_to_the_sentinels),
    ]))
