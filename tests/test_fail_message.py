#!/usr/bin/env python3
"""The failure notification: one line to read, one block to debug.

A failure message serves two people who are the same person in different
moods. Reading the phone, you want to know what happened in one line. Fixing
it afterwards, you want Kraken's exact words -- a paraphrase is useless there,
because the string you search for has to be the string Kraken sent.

The rule these tests defend: the original text is NEVER lost and never
rewritten. When there is no translation for a code, the sentence says to read
the block rather than inventing a cause.
"""
from _harness import kr, Runner

REAL = "limit AddOrder failed: ['EOrder:Insufficient funds']"


def t_code_extraction(r):
    C = kr.kraken_error_code
    r.check("out of the wrapped sentence", C(REAL), "EOrder:Insufficient funds")
    r.check("bare code", C("EAPI:Rate limit exceeded"), "EAPI:Rate limit exceeded")
    r.check("inside a list repr", C("['EService:Unavailable']"), "EService:Unavailable")
    r.check("no code at all", C("Connection timed out"), None)
    r.check("empty", C(""), None)
    r.check("none", C(None), None)


def t_translation(r):
    L = kr.kraken_error_lt
    r.check("the 07-30 one", L(REAL), "nepakanka lėšų")
    r.check("case insensitive", L("eorder:insufficient funds"), "nepakanka lėšų")
    # An unknown code must NOT be translated into something plausible.
    r.check("unknown code", L("EOrder:Something New"), None)
    r.check("not a kraken error", L("Connection timed out"), None)


def t_message_shape(r):
    # Assert on what Telegram actually receives, not on the intermediate form.
    lines = kr._tg_html(kr.msg_exec_fail(
        "2026-07-30", "KASUSD", "Nepavyko pateikti pavedimo.", REAL)).split("\n")
    r.check_true("titled", lines[0].endswith("<b>DCA KLAIDA</b>"))
    r.check("date and pair", lines[1], "2026-07-30 | KAS/USD")
    r.check("what was attempted", lines[2], "Nepavyko pateikti pavedimo.")
    r.check("cause in Lithuanian", lines[3], "Priežastis: nepakanka lėšų")
    r.check("blank line separates the debug block", lines[4], "")
    r.check("block is labelled", lines[5], "Kraken atsakymas:")
    r.check("original, in monospace", lines[6], "<code>EOrder:Insufficient funds</code>")


def t_unknown_error_keeps_the_original(r):
    # THE property: nothing is dropped and nothing is guessed.
    weird = "urlopen error [Errno 110] Connection timed out"
    m = kr.msg_exec_fail("2026-07-30", "KASUSD", "Nepavyko pateikti pavedimo.", weird)
    r.check_true("no invented cause", "Priežastis: žr. Kraken atsakymą žemiau." in m)
    r.check_true("original text survives verbatim", weird in kr._tg_html(m))


def t_empty_error_still_sends(r):
    # A notification that crashes on a blank error would lose the ONLY signal
    # that anything went wrong.
    for bad in ("", None, "   "):
        m = kr._tg_html(kr.msg_exec_fail("2026-07-30", "KASUSD", "Nepavyko.", bad))
        r.check_true(f"{bad!r} still renders", "<code>(be teksto)</code>" in m)


def t_lithuanian_writing_rules(r):
    # Roberto's two standing rules for anything he reads on the phone.
    for err in (REAL, "EAPI:Rate limit exceeded", "EGeneral:Permission denied",
                "EService:Busy", "totally unknown"):
        m = kr._tg_html(kr.msg_exec_fail("2026-07-30", "KASUSD",
                                         "Nepavyko pateikti pavedimo.", err))
        r.check(f"no em dash ({err[:18]})", "—" in m, False)
    # Every translation carries its diacritics: a Lithuanian word stripped of
    # them is the specific thing he asked never to see.
    bare = [v for v in kr.KRAKEN_ERROR_LT.values()
            if any(w in v.split() for w in
                   ("lesu", "uzraktas", "virsytas", "truksta", "mazesne", "uzimtas",
                    "uzklausu", "orderiu", "leidimu", "minimalu", "orderi"))]
    r.check("no undiacriticed Lithuanian", bare, [])


def t_html_is_escaped_inside_the_block(r):
    # The block shows text we did not write. It must not be able to inject
    # markup into a message Telegram parses as HTML.
    m = kr.msg_exec_fail("2026-07-30", "KASUSD", "Nepavyko.", "<b>fake</b> & <i>tags</i>")
    sent = kr._tg_html(m)
    r.check_true("our own bold survives", "<b>DCA KLAIDA</b>" in sent)
    r.check_true("our own code tag survives", "<code>" in sent)
    r.check("injected bold is neutralised", "<b>fake</b>" in sent, False)
    r.check_true("injected bold is shown as text", "&lt;b&gt;fake&lt;/b&gt;" in sent)
    r.check_true("ampersand escaped", "&amp;" in sent)


TESTS = [
    ("code extraction", t_code_extraction),
    ("translation", t_translation),
    ("message shape", t_message_shape),
    ("unknown error keeps the original", t_unknown_error_keeps_the_original),
    ("empty error still sends", t_empty_error_still_sends),
    ("Lithuanian writing rules", t_lithuanian_writing_rules),
    ("html escaped inside the block", t_html_is_escaped_inside_the_block),
]

if __name__ == "__main__":
    raise SystemExit(Runner("msg_exec_fail").run(TESTS))
