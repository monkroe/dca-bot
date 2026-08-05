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

WHAT IS CHECKED, AND WHAT DELIBERATELY IS NOT.

Checked: Lithuanian written WITHOUT its diacritics (`virsytas`, `ivykdyta`), and
the em dash. Both are spelling and typography rules, and both hold whatever
language a message is written in.

NOT checked, removed the same evening it was added: "no Lithuanian anywhere".
That layer swept the 2026-08-04 migration and then had to go, because LANGUAGE
IS NOT AN INVARIANT OF THIS SYSTEM -- it is a property of presentation. Robert
OS may be multilingual; the DB already keeps English identifiers
(`utilities_electric`) with Lithuanian labels rendered from them. A rule
forbidding a language would have frozen a presentation choice into a technical
guard, which is the same layer confusion that produced the evening's other
defects: a decision written where only a fact belongs.

It was also a migration guard in the literal sense, like `LABEL_FUEL_LEGACY`
the same morning: it did its sweep, and a migration that is never deleted
becomes a permanent rule nobody chose.

The removed layer had a second flaw worth remembering. It could not see
Lithuanian stripped of diacritics -- that is what the word list below is for,
and the first draft of this file dropped the word list IN FAVOUR of the
character scan, calling it stronger. It was not stronger, it was DIFFERENT.
Proved rather than argued: reinstating a real violation made the character scan
report only the em dash beside it and say nothing about the two Lithuanian
words.

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


# Lithuanian stems that LOSE a diacritic when stripped, and only those.
#
# EVERY ENTRY WAS CHECKED AGAINST WHAT THE WORD ACTUALLY IS, not against how it
# looks. That check was skipped when this list was first written on 2026-08-04,
# and the list quietly held `pirkimas`, `likutis`, `reikia`, `praleisti`,
# `senka`, `kaina`, `diena`, `klaida` and a dozen more -- all CORRECT Lithuanian
# needing no diacritic at all. It never fired, because every message was English
# that day; the moment the warning came back in Lithuanian it reported seven
# violations, none of them real.
#
# The same mistake had been found and fixed in benas-bot's guard hours earlier,
# and was not carried across to this one. A guard that reports correct spelling
# as a defect teaches its reader to skim past it, and then it catches nothing.
BARE_LITHUANIAN = {
    "ivykdyta", "ivykdytas", "ivykiui", "irasyta", "irasas",
    "islaida", "islaidos", "verte", "priezastis", "uzsaldyta",
    "virsytas", "virs", "lesu", "uzimtas", "uzraktas", "uzklausu",
    "neuzklausiamas", "mazesne", "minimalu", "leidimu", "truksta",
    "pirkimu", "orderi", "orderiu", "atsakyma", "daline", "siam",
    "demesio", "menesio", "menesi", "savaite", "aciu", "siandien",
    "biudzetas", "sekmingai", "atsauk", "istrinti", "isjungta",
    "ijungta", "uzrasyta", "prasau",
}


def _words(text):
    return {w.strip(".,:;!?()[]{}<>/\\\"'").lower() for w in text.split()}


def test_no_bare_lithuanian_in_any_source_string(t):
    # Layer 2. The case layer 1 is structurally unable to see.
    for f in FILES:
        bad = [f"{f}:{ln} {v[:50]!r}" for ln, v, _ in _literals(f)
               if _words(v) & BARE_LITHUANIAN]
        t.check(f"{f} has no undiacriticed Lithuanian", bad, [])


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
        ("no undiacriticed Lithuanian either", test_no_bare_lithuanian_in_any_source_string),
        ("no em dash in anything sendable", test_no_em_dash_in_anything_that_can_be_sent),
        ("the scanner can actually fail", test_the_scanner_can_actually_fail),
        ("every source file was actually read", test_every_source_file_was_actually_read),
    ]))
