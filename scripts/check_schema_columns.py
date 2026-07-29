#!/usr/bin/env python3
"""Fail the build when a write names a column the table does not have.

WHY. The benas bot shipped two inserts naming a column that does not exist;
PostgREST rejects the whole row and the caller sees a generic failure, so one of
them wrote nothing for weeks without anyone noticing. This repo writes through
the same API and is exposed to the same silence -- worse, because these writes
carry money. A typo in a `dca_executions` update would not raise: the row would
simply never record the fill.

Nothing else in the pipeline sees it. `py_compile` is happy with any dict, and
the unit tests exercise pure decision functions that never touch Supabase.

This uses `ast`, not regular expressions: the payloads are real Python dict
literals, so they can be parsed properly rather than pattern-matched. Anything
not a plain literal -- a variable, a comprehension, a `**merge` -- is skipped
rather than guessed at. A check that produces false alarms gets switched off,
and a switched-off check is worse than none because it looks like coverage.

REGENERATING db/schema-columns.json after a migration:

    select json_build_object('tables', json_object_agg(table_name, cols))
    from (
      select table_name, json_agg(column_name order by ordinal_position) as cols
      from information_schema.columns
      where table_schema = 'public'
        and table_name = any (array[ ...the tables this bot writes to... ])
      group by table_name
    ) t;

Committed rather than queried live because this runs in CI, which has no
database credentials.
"""
import ast
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SNAPSHOT = ROOT / "db" / "schema-columns.json"
SOURCES = sorted((ROOT / "src").glob("*.py"))

# helper -> index of the argument carrying the column payload
PAYLOAD_ARG = {
    "sb_insert": 1,   # sb_insert(table, row)
    "sb_update": 2,   # sb_update(table, match_params, updates)
    "sb_upsert": 1,   # sb_upsert(table, rows, on_conflict)
}
# sb_update's match_params are PostgREST filters keyed by column, so they are
# worth checking too -- a filter on a misspelled column silently matches nothing
# and the update quietly does nothing at all.
FILTER_ARG = {"sb_update": 1}

SKIP_FILTER_KEYS = {"select", "order", "limit", "offset", "on_conflict"}


def literal_keys(node: ast.AST) -> list[str] | None:
    """Keys of a dict literal, or None when the node is not one we can read."""
    if not isinstance(node, ast.Dict):
        return None
    keys = []
    for k in node.keys:
        if k is None:            # {**other} -- nothing to check here
            continue
        if isinstance(k, ast.Constant) and isinstance(k.value, str):
            keys.append(k.value)
    return keys


def collect_dict_vars(tree: ast.AST) -> dict[str, list[str]]:
    """Names bound to a dict literal, plus keys added later by `name["k"] = ...`.

    The payload is often built as a variable and passed afterwards -- `claim_row`
    in kraken_run.py is the clearest case, and it is the most important write in
    the bot, the row that reserves the day's purchase. Checking only the
    arguments that are literals at the call site left exactly that one
    unexamined, which mutation-testing this script is what revealed.

    Resolving a name can only ADD keys to inspect, never invent one, so it
    cannot produce a false alarm. A name bound more than once is dropped rather
    than merged: two shapes under one name is precisely where a guess would be
    wrong.
    """
    seen: dict[str, list[str] | None] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            t = node.targets[0]
            if isinstance(t, ast.Name):
                keys = literal_keys(node.value)
                if keys is None:
                    seen[t.id] = None           # rebound to something unreadable
                elif t.id in seen:
                    seen[t.id] = None           # two shapes, one name: do not guess
                else:
                    seen[t.id] = list(keys)
            elif (isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name)
                  and isinstance(t.slice, ast.Constant) and isinstance(t.slice.value, str)):
                cur = seen.get(t.value.id)
                if isinstance(cur, list):
                    cur.append(t.slice.value)
    return {k: v for k, v in seen.items() if isinstance(v, list)}


def called_name(node: ast.Call) -> str | None:
    f = node.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return None


def main() -> int:
    if not SNAPSHOT.exists():
        print(f"FAIL: missing {SNAPSHOT.relative_to(ROOT)}")
        return 1
    tables = json.loads(SNAPSHOT.read_text(encoding="utf-8"))["tables"]

    problems, checked = [], 0
    for path in SOURCES:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        dict_vars = collect_dict_vars(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = called_name(node)
            if name not in PAYLOAD_ARG:
                continue
            if not node.args:
                continue
            first = node.args[0]
            if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
                continue
            table = first.value
            known = tables.get(table)
            if known is None:
                continue

            for arg_index, is_filter in ((PAYLOAD_ARG[name], False),
                                         (FILTER_ARG.get(name, -1), True)):
                if arg_index < 0 or arg_index >= len(node.args):
                    continue
                arg = node.args[arg_index]
                keys = literal_keys(arg)
                if keys is None and isinstance(arg, ast.Name):
                    keys = dict_vars.get(arg.id)
                if keys is None:
                    continue
                checked += 1
                for key in keys:
                    if is_filter and key in SKIP_FILTER_KEYS:
                        continue
                    if key not in known:
                        kind = "filter" if is_filter else "payload"
                        problems.append((path.name, node.lineno, table, name, kind, key))

    if problems:
        print("FAIL: columns that do not exist on the target table:")
        for fname, line, table, fn, kind, key in problems:
            print(f"  {fname}:{line}  {fn}(\"{table}\") {kind}  ->  {key}")
        print("\nEither the column is misspelled, or the snapshot is stale after a")
        print("migration. Regenerate db/schema-columns.json (see this script's header).")
        return 1

    print(f"OK: {checked} write payloads/filters, every column exists")
    return 0


if __name__ == "__main__":
    sys.exit(main())
