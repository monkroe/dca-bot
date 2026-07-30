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

Committed rather than queried live because the check must run without database
credentials.

WHERE THIS RUNS, stated accurately after getting it wrong once: `test.sh`, and
`test.sh` alone. **This repository has no CI workflow that runs it** -- the four
workflows here execute the bot, not the tests. So this gate protects only what
someone remembers to run locally. The changelog and STATUS first claimed it runs
in CI; that was false and is corrected. A gate whose execution is not guaranteed
is a gate for the runs where it happened to be invoked.
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


def scopes(tree: ast.Module) -> list[list[ast.AST]]:
    """Each top-level function as its own scope, plus module level.

    Names had been collected across the WHOLE file, which is wrong in both
    directions. `rows` is the payload variable in all four `kraken_sync`
    sources; merged, its keys would be the union of four different tables and
    every one of them would be flagged for the others' columns -- the false
    alarm that gets a check switched off. Merged the other way, a name bound
    twice was dropped, so a payload that was perfectly checkable went
    unexamined. Scoping fixes both: a nested function is walked with its
    parent, which is what closures like `update_execution` need.
    """
    out, module_rest = [], []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.append([node])
        else:
            module_rest.append(node)
    out.append(module_rest)
    return out


def collect_list_vars(nodes: list[ast.AST]) -> dict[str, list[str]]:
    """Names bound to a list that is filled with dict literals by `.append`.

    `sb_upsert(table, rows, on_conflict)` takes a LIST of rows, and every
    source in `kraken_sync` builds it exactly this way: `rows = []` followed by
    `rows.append({...})` inside a loop. None of those writes were checked --
    the payload argument was a name bound to a list, so the gate skipped it and
    the whole mirror sat outside the check that exists to protect it.

    Keys are UNIONED across appends: each append is one row, and every key in
    every row has to exist. That is the opposite of the rule for dict
    variables, where two shapes under one name means do not guess -- here two
    shapes are two rows going to the same table, and both must be valid.
    """
    seen: dict[str, list[str] | None] = {}
    for root in nodes:
        for node in ast.walk(root):
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                t = node.targets[0]
                if isinstance(t, ast.Name) and isinstance(node.value, ast.List):
                    keys = []
                    for el in node.value.elts:
                        k = literal_keys(el)
                        if k is None:
                            keys = None
                            break
                        keys.extend(k)
                    seen[t.id] = keys
                elif isinstance(t, ast.Name) and isinstance(node.value, ast.ListComp):
                    # `rows = [{...} for x in y]` -- sync_balances builds its
                    # payload this way and was missed by the first version of
                    # this, which only understood `.append`. Every row a
                    # comprehension yields has the same shape, so its keys can
                    # be read straight off the element.
                    seen[t.id] = literal_keys(node.value.elt)
            elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                  and node.func.attr == "append" and isinstance(node.func.value, ast.Name)
                  and node.args):
                cur = seen.get(node.func.value.id)
                if isinstance(cur, list):
                    k = literal_keys(node.args[0])
                    if k is None:
                        seen[node.func.value.id] = None
                    else:
                        cur.extend(k)
    return {k: sorted(set(v)) for k, v in seen.items() if isinstance(v, list) and v}


def collect_dict_vars(nodes: list[ast.AST]) -> dict[str, list[str]]:
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
    for root in nodes:
        for node in ast.walk(root):
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                t = node.targets[0]
                if isinstance(t, ast.Name):
                    keys = literal_keys(node.value)
                    if keys is None:
                        seen[t.id] = None       # rebound to something unreadable
                    elif t.id in seen:
                        seen[t.id] = None       # two shapes, one name: do not guess
                    else:
                        seen[t.id] = list(keys)
                elif (isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name)
                      and isinstance(t.slice, ast.Constant) and isinstance(t.slice.value, str)):
                    cur = seen.get(t.value.id)
                    if isinstance(cur, list):
                        cur.append(t.slice.value)
    return {k: v for k, v in seen.items() if isinstance(v, list)}


def keys_of(arg: ast.AST, dict_vars: dict, list_vars: dict) -> list[str] | None:
    """Every column an argument names, or None when it cannot be read.

    One place instead of three, because the three used to disagree: an argument
    written `[row]` -- a list holding a NAME, which is how `write_state` calls
    `sb_upsert` -- fell between the literal branch and the name branch and was
    checked by neither. Resolving recursively removes the gap rather than
    adding a fourth special case beside it.
    """
    keys = literal_keys(arg)
    if keys is not None:
        return keys
    if isinstance(arg, ast.Name):
        return dict_vars.get(arg.id) or list_vars.get(arg.id)
    if isinstance(arg, ast.List):
        out = []
        for el in arg.elts:
            k = keys_of(el, dict_vars, list_vars)
            if k is None:
                return None
            out.extend(k)
        return out
    return None


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
        for scope in scopes(tree):
            dict_vars = collect_dict_vars(scope)
            list_vars = collect_list_vars(scope)
            for node in [n for root in scope for n in ast.walk(root)]:
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
                    keys = keys_of(node.args[arg_index], dict_vars, list_vars)
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
