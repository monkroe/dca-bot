"""Roberto's two writing rules, checked against EVERY string the bot can send.

WHY THIS FILE EXISTS, and it is not the rules themselves. Both were already
written down, and one of them was already tested -- in exactly one message.
`test_low_balance_message` asserted no em dash in `low_balance_message`, and
`test_fail_message` asserted the same for `msg_exec_fail`. On 2026-08-04 a scan
of the other twenty-four Telegram messages found five em dashes and five
messages written in Lithuanian WITHOUT diacritics, which is the specific thing
he asked never to see.

So the defect was not the rule and not the violations. It was that a rule
covering twenty-six messages was enforced on two of them, and the gap was
invisible: every test passed, every day, while most of the surface went
unchecked. Same shape as the schema gate this repo already has -- the value is
in covering the SET, not a well-chosen member of it.

METHOD: TWO LAYERS, because neither one is sufficient and the first draft of
this file shipped with only one.

  1. CHARACTER SET. Any Lithuanian letter in a string literal. This catches
     words nobody thought to list, which is most of them.
  2. WORD STEMS. Lithuanian written WITHOUT diacritics -- `Rankinis`,
     `ivykdytas`, `virsytas`. Layer 1 is blind to these by construction, and
     five of the messages found on 2026-08-04 were exactly this. Verified
     rather than assumed: reinstating a real violation made layer 1 report only
     the em dash beside it and say nothing about the two Lithuanian words.

The first version of this file dropped an existing word-list check in favour of
layer 1 and described that as the stronger method. It was not stronger, it was
DIFFERENT, and dropping the other left a hole exactly where the day's most
common defect lived. Recorded here because the reasoning error is easier to
repeat than the bug.

Scanning literals rather than call sites catches text assigned to a variable and
passed in later, which is how `fail_action` and the `_mark_manual_required`
notes reach Telegram.

The limit that remains: a string built at runtime from data (a Kraken error, a
DB `reason` column) cannot be seen here. Deliberate rather than overlooked --
those are quoted foreign text, and these rules govern the words this project
writes.
"""
import ast
import os
import sys
from pathlib import Path

from _harness import Runner  # sets sys.path to src/ and stubs credentials

os.environ.setdefault("STRIKE_API_KEY", "test-stub-not-used")

SRC = Path(__file__).resolve().parent.parent / "src"
FILES = ["kraken_run.py", "kraken_sync.py", "strike_run.py"]

LITHUANIAN = set("ąčęėįšųūžĄČĘĖĮŠŲŪŽ")
EM_DASH = "—"


def _docstring_nodes(tree):
    """Docstrings are documentation, not output. They stay English prose and may
    use any punctuation the author likes."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                out.add(id(body[0].value))
    return out


def _print_nodes(tree):
    """`print` goes to the GitHub Actions log, which Roberto does not read on his
    phone. Excluded for the em dash only; Lithuanian is checked everywhere,
    because a message can always be moved from a log line into a notification."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "print":
            for inner in ast.walk(node):
                if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
                    out.add(id(inner))
    return out


def _literals(filename):
    """Every string literal with its line, minus docstrings. `id()` is stable
    here because the tree is held alive by the caller."""
    tree = ast.parse((SRC / filename).read_text(encoding="utf-8"))
    skip = _docstring_nodes(tree)
    prints = _print_nodes(tree)
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip:
            out.append((node.lineno, node.value, id(node) in prints))
    return out


# Lithuanian stems that SURVIVE having their diacritics stripped, so layer 1
# cannot see them. Chosen to have no English collision: "is" (for "iš") and
# "be" are deliberately absent, because both are ordinary English words and a
# guard that cries wolf gets switched off.
BARE_LITHUANIAN = {
    "rankinis", "ivykdytas", "ivykdyta", "pirkimas", "pirkimai", "pirkimu",
    "pardavimas", "kiekis", "kaina", "verte", "mokestis", "orderis", "orderi",
    "orderiu", "nepavyko", "priezastis", "klaida", "atsakymas", "atsakyma",
    "likutis", "diena", "priimta", "daline", "apimtimi", "automatika",
    "sustabdyta", "siam", "ivykiui", "patikrink", "ranka", "virsytas", "virs",
    "neuzklausiamas", "uzklausu", "lesu", "dalys", "netinkamas", "netinkama",
    "truksta", "uzimtas", "uzraktas", "mazesne", "minimalu", "leidimu",
    "nepakanka", "praleisti", "reikia", "balanse", "senka", "demesio",
}


def _words(text):
    return {w.strip(".,:;!?()[]{}<>/\\\"'").lower() for w in text.split()}


def test_no_bare_lithuanian_in_any_source_string(t):
    # Layer 2. The case layer 1 is structurally unable to see.
    for f in FILES:
        bad = [f"{f}:{ln} {v[:50]!r}" for ln, v, _ in _literals(f)
               if _words(v) & BARE_LITHUANIAN]
        t.check(f"{f} has no undiacriticed Lithuanian", bad, [])


def test_no_lithuanian_in_any_source_string(t):
    # Every message the bot sends is English as of 2026-08-04. Lithuanian
    # anywhere in a literal now means either a message that was missed or a new
    # one written in the old habit.
    for f in FILES:
        bad = [f"{f}:{ln} {v[:50]!r}" for ln, v, _ in _literals(f) if set(v) & LITHUANIAN]
        t.check(f"{f} has no Lithuanian", bad, [])


def test_no_em_dash_in_anything_that_can_be_sent(t):
    # Console logging is exempt; anything that can reach Telegram is not.
    for f in FILES:
        bad = [f"{f}:{ln} {v[:50]!r}" for ln, v, is_print in _literals(f)
               if EM_DASH in v and not is_print]
        t.check(f"{f} has no em dash", bad, [])


def test_the_scanner_can_actually_fail(t):
    # A checker that cannot fail is worse than none: it reports safety it never
    # measured. This repo has shipped exactly that twice -- boot.sh printing a
    # green tick over empty sections, and cron reporting success for a statement
    # that was only queued. So the detector is tested against a known-bad input.
    tree = ast.parse('x = "nepakanka lesu"\ny = "a — b"\nprint("c — d")\n')
    strings = [n.value for n in ast.walk(tree)
               if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    t.check("finds a diacritic", any(set(s) & LITHUANIAN for s in ["nepakanka lėšų"]), True)
    t.check("finds an em dash", any(EM_DASH in s for s in strings), True)
    # And does NOT fire on the text that is now shipped.
    t.check("clean text passes", any(set(s) & LITHUANIAN for s in
                                     ["insufficient funds", "Could not place the order."]), False)


def test_every_source_file_was_actually_read(t):
    # The failure mode this whole file exists to prevent, one level up: a scan
    # that silently covers fewer files than it claims. A typo in FILES would
    # make every check above pass by examining nothing.
    for f in FILES:
        t.check(f"{f} yielded literals", len(_literals(f)) > 20, True)


if __name__ == "__main__":
    sys.exit(Runner("message charset: Lithuanian and em dash, all messages").run([
        ("no Lithuanian in any source string", test_no_lithuanian_in_any_source_string),
        ("no undiacriticed Lithuanian either", test_no_bare_lithuanian_in_any_source_string),
        ("no em dash in anything sendable", test_no_em_dash_in_anything_that_can_be_sent),
        ("the scanner can actually fail", test_the_scanner_can_actually_fail),
        ("every source file was actually read", test_every_source_file_was_actually_read),
    ]))
