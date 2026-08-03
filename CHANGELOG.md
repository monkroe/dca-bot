# DCA Bot -- CHANGELOG

Conventions: dates are **Chicago** time (the bot's trading timezone); a "vakaras" entry may already be the next day in UTC. No concrete money amounts in this file -- rates, mechanisms, dates and counts only; amounts live in the DB and the bot's own messages. Strategy/design rationale lives in `robert-os-hub/docs/05-roadmap/dca-phase2-*.md`; this file records what shipped.

History before 2026-07-18 (Phase 1 -- Kraken + Strike execution, notifications, reconciliation, impact/all-in bps telemetry) is in `git log`; this changelog starts at Phase 2.

## 2026-08-03 (pirmadienis -- Chicago)

### fix(notify): the low-balance warning named the money it was about to spend -- v1.7.6
- It fired in the buy preflight, four minutes before the purchase. On 08-03 it reported enough for roughly one more day; the answer that mattered, after that morning's fill, was **zero more buys**. Roberto found it by comparing the message against his own account, not from any check
- Useless twice over: the number was wrong by exactly one buy, and four minutes is not enough time to move money. Removed from the preflight entirely
- **The number now appears where it is true.** The FILL notification gained one line -- `Liko: <balance> (N pirkimų)` -- costing no new message, because it lands where Roberto is already reading. The COUNT is the point: a bare balance does not say the next buy fails, and `(0 pirkimų)` does
- **The warning moved to the EVENING run of `kraken_sync`**, roughly nine hours before the next buy window. Measured reason for the evening rather than midday: of July's 23 shift payouts, **89% by value arrived after 16:00**, so a midday warning asks for a top-up out of income that does not exist yet. The evening is the last point at which a transfer still changes tomorrow's outcome
- **Escalation, because the old message read identically at 4.9 days and at 0.2 days.** Below one day it is not "running low", it is a scheduled failure with a time on it, and it now says so: `RYTOJ DCA PIRKIMAS NEPAVYKS`
- **One warning path, so no day key is needed.** Three callers would have sent three messages -- two of them identical -- and a warning that repeats stops being read. The hour guard (`>= 20:00` Chicago) is the whole deduplication, written as a range rather than an exact hour because the pg_cron schedule is UTC and drifts with DST
- `_buys_word` is tested (20 assertions): the Lithuanian plural has three branches and two exceptions, and it is wrong in a way nobody reports -- "0 pirkimas" is not broken, only foreign, and gets read past
- Caught while writing it, by a mechanical name check rather than by review: the evening guard first used `ZoneInfo` without importing it. It compiled, and would have raised `NameError` inside a `try` that swallows -- a warning silently never sent. Now uses `kr.CHICAGO_TZ`, the single definition that already exists

## 2026-08-02 (sekmadienis -- Chicago)

### fix(mirror): the snapshot stored what is held, never what is spendable -- v1.7.5
- `kraken_balances` gained `hold_trade` (`db/v11`), and both snapshot writers now ask `BalanceEx` first, which is the only shape that reports it. `Balance` stays as the fallback: a snapshot without the held amount beats no snapshot
- The gap was mine and it was in the same file as its own explanation. `balance_rows` read `hold_trade` out of the BalanceEx dict and then dropped it, while both callers were still passing `Balance` -- which has no such field, so the discard was invisible. The preflight has used BalanceEx since 07-30; only the mirror had not caught up
- **NULL, never 0.0, when the shape cannot carry it.** "Not known at this snapshot" and "nothing was held" are opposite claims on the only day this matters, and a zero would have made the blind snapshot look like a healthy one. Rows written before v11 keep NULL honestly -- they were taken with `Balance`
- Found while re-investigating the 07-30 refusal from scratch, which was itself the mistake: `db/v10`'s own header already stated the cause in one sentence. The mirror answers this question now; the documentation already did

## 2026-08-01 (šeštadienis vakaras -- Chicago; UTC jau 08-02)

### feat(mirror): the run takes the snapshot, because the run is when it matters -- v1.7.4
- `kraken_open_orders` was created to answer "what was my money committed to at 06:53" -- the question the 07-30 refusal could not be answered from. Its sync then ran once a day at 15:00, so the table could never observe the moment it exists for. A week later it held exactly one snapshot. For a point-in-time table cadence is not a detail, it IS the information
- The trading run already happens at that moment, so it now writes one balance and one open-orders snapshot itself. No new scheduled job, two extra API calls
- Placed in the preflight, BEFORE the funding branch, so the record exists on both outcomes. The case that motivated the table was a refusal, not a buy; a snapshot only on success would have missed exactly the day it was needed
- **Once per day, not once per invocation.** Cron fires this process every five minutes through the window, but the preflight sits behind the day's claim, so later invocations return before reaching it. In `main()` it would have written roughly 120 snapshots a day for the same information
- Wrapped whole and never raises. A snapshot is telemetry; the buy must not be able to fail because of it
- Row shape extracted to `balance_rows` / `open_order_rows` and shared with `kraken_sync` (v1.1.1), which had its own copy. Two builders for one table is two chances to disagree about it
- `balance_rows` accepts BOTH shapes Kraken returns: `Balance` gives a scalar per asset, `BalanceEx` a dict with `balance` and `hold_trade`. The sync calls the first and the trading path the second, and a builder that understood only one would write an empty snapshot for the other while reporting success
- **The schema gate silently lost coverage in this change** and the count is the only thing that said so: 60 payloads before, 58 after. It resolves dict literals at the call site, and these rows now arrive from a function it cannot follow. Covered instead by `tests/test_snapshot_rows.py`, which checks the built keys against `db/schema-columns.json` directly -- stronger than the gate, because it proves the dicts that actually get written
- 9 branches / 20 assertions, and the gate was shown able to go RED by two mutations (average fill price instead of the limit; dropping the non-zero filter). Source restored, diff clean

### chore(sync): second daily run, 01:00 and 13:00 Chicago
- `kraken-sync` was `0 20 * * *`, one run a day. Anything done by hand in the evening waited until the next afternoon to appear -- a sale at 23:40 was invisible for fifteen hours
- Now `0 6,18 * * *`. One job with two times rather than a second job: the cron health gap is still open, and every additional job is another thing that can stop without saying so
- **The second hour was measured, not picked.** The first attempt kept the old 15:00 and simply added 01:00. Roberto asked whether 13:00 would be better -- twelve hours apart -- and the answer is yes, for a different reason than the symmetry that prompted it. Against 45 days of real ledger events, mean staleness falls from 6.39 h to 5.43 h and the WORST case from 12.68 h to 10.68 h. 13:00 gives the lowest worst case of any hour tested, because it lands just after the midday cluster of activity rather than two hours later
- 11:00 scores a slightly better mean (5.07 h) and a worse worst case (12.76 h). The worst case is what went wrong here, so it won the tie
- Deliberately NOT higher frequency. Trades and ledgers are immutable history, so more often buys freshness and no information; balances would add roughly 78,000 rows a month to a free-tier database. Roberto declined it for those reasons

## 2026-07-31 (penktadienis -- Chicago)

### fix(telemetry): the fill overwrote the very record v1.7.2 added -- v1.7.3
- First live run of v1.7.2 and the preflight reading was not there. `finalize_order` replaced `raw` wholesale with the Kraken order result, so the balance source written at `limit_open` was gone by the time anyone could read it. The commit message for v1.7.2 said the evidence has to outlive the run; it did not outlive the run
- **Third time this trap has been walked into.** The retry counter could not live in `raw` because the failure handler replaces it; the re-peg count could not, for the same reason; and now the preflight reading. Twice it was noticed while writing the code and routed around. This time it was not, in the one commit whose entire purpose was durable evidence
- Fixed at the WRITE rather than remembered at each caller: `finalize_order` now merges into whatever `raw` already holds. Every other writer to that column was already merging or deliberately replacing on a terminal failure
- Verified first: mid_source, ref_mid and the impact figures were all correct on the same row, so this was the raw column alone and not the telemetry path

### VERIFICATION -- 2026-07-31 06:58, three pendings on one run
- **P2 PASS.** `mid_source = 'snapshot'`, impact +3.6 bps against a snapshot mid rather than a ten-minute-old ticker. v1.5.2 confirmed on the live path
- **P3 fails to answer, for the reason above.** The mechanism itself is untested until tomorrow: whether the spendable-balance preflight ran cannot be told from a row whose evidence was overwritten
- **P1 INCONCLUSIVE, and the query said FAIL.** The leg was placed 06:53:19, filled by Kraken 06:58:03 and inspected 06:58:11, by which time it was closed. `run_maker_inspection` finalizes a closed order and continues before reaching `_maybe_repeg`, which is the only caller of the probe. So zero probe rows is correct here, not a fault. The verification query treated `filled` as proof a leg had been inspected while open; corrected in the hub roadmap
- A query written to remove ambiguity introduced a different one, and it took reading the code to tell a real failure from an expected silence

## 2026-07-30 (ketvirtadienis -- Chicago, session 22)

### fix(telemetry): a successful run now records WHICH balance source it used -- v1.7.2
- v1.7.0 records the spendable/held reading when a buy fails, and nothing when it succeeds. So "the spendable-balance preflight is live" could only be shown from an Actions log that rotates -- and tomorrow's 06:03 verification is precisely a case where the evidence has to outlive the run
- The `limit_open` row now carries `raw.preflight` with the reading and the source. Additive: the re-peg machinery reads its own keys out of the same `raw` and is unaffected
- The post-only rejection path also records the request now, like the other three AddOrder failures did from v1.7.0

### feat(messages): the failure notification separates the sentence from the evidence -- v1.7.1
- Roberto, on the 07-30 message: as the administrator he needs the ORIGINAL error text for debugging, and it should be set apart visually. The old message pasted Kraken's raw reply into the sentence, so it was bad at both jobs -- unreadable at a glance, and buried at the moment it mattered
- Now a line to read and a block to debug: what was attempted, the cause in Lithuanian, then Kraken's untouched reply in its own monospace block. Applied to all five failure notifications, not only the one he saw
- **An unrecognised code is never translated.** The sentence says to read the block instead, and the original text is shown verbatim. Inventing a plausible cause for an unknown failure is how a message ends up lying about what went wrong
- 15 Kraken codes carry a Lithuanian cause, with diacritics -- a test fails the build on a Lithuanian word stripped of them, and on an em dash in any rendered message

### fix(telegram): the tag allowlist matched spelling, not origin
- `_tg_html` escaped the whole message and then turned `&lt;b&gt;` back into `<b>`, which restores the tag WHEREVER it appears -- including inside text the bot did not write. Harmless while every message was built from our own words; it stopped being harmless the same commit that started showing an external reply verbatim
- Formatters now emit control-character markers that are converted to tags AFTER escaping, so a tag can only come from a formatter. The allowlist is about where the markup came from, which is what it was always meant to be
- Found by a test written for the new message, not by review. `strike_run.py` carries its own copy of the old scheme; its messages are built entirely from our own strings, so the flaw is latent there rather than live, and it is left alone rather than changed unasked

### fix(exec): two defects found while writing the tests
- The Kraken code was pulled out with `E[A-Za-z]+:`, which matched "failed:" inside our own wrapper sentence once the match was made case-insensitive and reported the error code as `ed:`. Anchored on the real Kraken error classes
- The lookup was case-sensitive against a lower-cased table, so a reason stored in a different case had no translation at all

### fix(exec): a leg Kraken refused can be attempted again while the window is still open -- v1.6.0
- **What happened this morning.** The maker leg was refused with `EOrder:Insufficient funds`. The window was still open and usable funds were on the account, but every later cron cycle in that window returned `already_claimed` and did nothing. The day was lost, and lost during the Phase 2 trial period, so it also damages the statistics the 08-11 verdict is read from
- **Why `already_claimed` fired.** In live maker mode `cl_ord_id` is deterministic (`dca-PAIR-DATE-HHMM`), so the next cycle re-inserted the identical id, hit the unique constraint, and returned early. That guard is correct and stays -- it is what prevents a double buy. What was missing was a path through it
- A retry now takes over the SAME row and rotates the client id, exactly as the re-peg path already does. `dca_exec_leg_per_event_uniq` still forbids a second `maker_limit` row per order per day, so the I3 double-buy guard is untouched; `parent_event_id` is carried over, keeping the event lineage intact
- **THE LINE THAT MUST NOT MOVE (DP-3): retry is allowed only when Kraken said no.** An explicit rejection means no order was created and re-attempting is safe. A timeout or a dropped connection is NOT a rejection -- the order may be live, and retrying it would buy twice. So the trigger is an ALLOWLIST of explicit rejections, not a denylist of failures: an unrecognised error is treated as unknown, and unknown never retries
- Bounded three ways: at most three attempts, only inside the window (deadline pulled back by one cron cycle so a retry can never place an order the window would not have allowed), and only from `failed_kraken`
- **The attempt counter lives in the client id, not in `raw`.** `raw` was the obvious place and is the wrong one: the failure handler REPLACES `raw` on every rejection, so a counter kept there would reset to zero on the very event that increments it and the cap would never be reached. The id is written once per attempt and never overwritten
- `tests/test_retry_decision.py`, 9 branches / 44 assertions, wired into `test.sh`. **Verified able to go RED:** eight mutations of the live source -- drop the status guard, flip the window comparison, loosen the cap to `>`, bypass the allowlist, remove case folding, `rpartition` to `partition`, drop the digit check, and an off-by-one in the count -- each turn it red. The first version of the tests survived two of those and was strengthened until none survived. Source restored, diff empty
- **This alone would not have bought this morning.** If the funds are genuinely locked, the retry hits the same refusal and stops after three. It removes the structural block, not the funding one. Three known gaps stay open: the preflight reads the TOTAL balance rather than the available one, so it waves through an order Kraken will refuse; the failure `raw` records only the error and not the request that caused it; and the Kraken mirror has no OpenOrders, so what the funds were locked in is not visible from the database
- The preflight change below can now SKIP a buy that v1.6.0 would have attempted, so it is a purchase-decision change and takes the version to 1.7.0. v1.6.0 existed only between two commits this morning and never ran a window
- **Also fixed: `VERSION` was never bumped for v1.5.2.** The commit and this changelog both call it v1.5.2 while the constant still said 1.5.1, so every Actions log from that fix printed the wrong version -- which is the one thing the constant exists for. Past logs cannot be corrected; the constant now reads 1.6.0

### fix(exec): the preflight now reads what can be SPENT, not what is owned -- v1.7.0
- `check_balance_usd` said "available" in its docstring and called `Balance`, which is the TOTAL -- it counts USD already committed to resting orders. That is the lie that cost 07-30: the preflight saw enough, waved the order through, and Kraken refused it. A preflight that cannot fail before the exchange does is not a preflight
- Now `BalanceEx`, which returns `balance` and `hold_trade` per asset, so spendable = balance - hold_trade (verified against the Kraken REST reference, not inferred). The run log and the skip message both show what is held
- **Falls back to `Balance` instead of blocking the buy.** `BalanceEx` may need a permission this key does not carry, and a funding CHECK must never be the thing that stops trading. The fallback returns the old optimistic number, so the SOURCE is returned with it and printed -- a degraded check that looks identical to a good one is how the first version went wrong

### fix(exec): a failure now records the request that caused it
- Failures stored `{"error": ...}` and nothing else, so the file said what Kraken thought of a request nobody kept. On 07-30 an `EOrder:Insufficient funds` sat there with no volume, no price and no balance beside it, and every question worth asking had to be reconstructed from an Actions log that rotates
- All four `AddOrder` paths now record the request next to the error, plus the spendable and held balances at that moment. The nonce is stripped and no credential can reach the row -- the key and signature are added inside `kraken_private` and never appear in these params, and a test asserts on the serialised string so anything smuggled in through the context argument is caught too
- **The re-peg path MERGES rather than replaces.** Its `raw` carries `repeg_count` and the history; overwriting it would reset the count and the leg would re-peg forever. Same trap as the retry counter, one function along

### feat(mirror): the fourth source -- orders that are still resting
- The mirror covered Balance, TradesHistory and Ledgers, all of which describe money that has ALREADY moved. An open order is money that has not moved and cannot be spent -- exactly the state that caused the refusal, and the one state with no record anywhere
- **CORRECTION, same evening.** This entry first said OpenOrders needs no permission beyond Balance because the trading path already calls it. The first live sync returned permission denied: the trading key can call it, the READ-ONLY key this job runs on cannot, and "Query open orders & trades" is a separate tick from "Query closed orders & trades". Reasoning from what one key can do said nothing about the other. The per-source design contained it -- balances, trades and ledgers all synced normally and only this one is blocked
- Until that box is ticked the mirror stays empty. The at-failure digest in the trading path is unaffected: it runs on the trading key, which has the permission
- `db/v10-kraken-open-orders.sql`, applied. Append-only snapshots like `kraken_balances`, because the question is never only "what is resting now" but "what was resting at 06:53". RLS ON with no policies, matching the posture the other four were put into on 07-29; verified by reading as `anon` and getting zero rows back while the row existed
- USD locked is derived, not stored: `(vol - vol_exec) * price`. A stored number that can disagree with its own inputs is worse than an absent one
- **The trap, since it is silent:** `descr.price` is the limit price; the top-level `price` is the average FILL price and reads 0 on an untouched order. Taking the wrong one values every resting order at nothing. Pinned by a test
- Since the sync workflow is manual, the trading path also captures the open-order book INTO the failure record at the moment of a refusal or a funding skip -- the only place the timing is guaranteed to line up. It never raises: a diagnostic that can turn a failure into a crash is worse than no diagnostic

### test: the schema gate was blind to the entire mirror
- `sb_upsert` takes a LIST of rows, and every source in `kraken_sync` builds it as `rows = []` then `rows.append({...})`, or as a comprehension. The gate only understood dict literals and names bound to them, so it skipped all of it -- the mirror sat outside the check that exists to protect it, and `write_state`'s `[row]` form fell between two branches and was checked by neither
- Names are now resolved per FUNCTION rather than per file. Merged across a file, `rows` would have been the union of four different tables and every one flagged for the others' columns -- the false alarm that gets a check switched off; merged the other way, a name bound twice was dropped and a perfectly checkable payload went unexamined
- List keys are UNIONED across appends (each append is a row and every key must exist), which is the opposite of the rule for dict variables, where two shapes under one name means do not guess
- 50 payloads checked before today, 60 now. Nine mutations, one per write path in both modules, each turn it red

### test: a stale `__pycache__` reported a pass for code that was not run
- Two mutations of the live source came back GREEN while the mutated code was broken: the cache held the previous bytecode and the import never saw the change. `test.sh` happens to be safe -- `py_compile` rewrites the cache from current source before anything imports it -- but nothing said so, and the ad-hoc mutation runs that skip that step were not safe at all. Both mutations go red once the cache is cleared, and all 17 of today's were re-run that way
- `PYTHONDONTWRITEBYTECODE=1` in `test.sh`, with the reason written above it
## 2026-07-28 (antradienis -- Chicago, session 21)

### test: the build fails when a write names a column that does not exist
- The same gate as benas-bot, and it matters more here because these writes carry money. A misspelled key in a `dca_executions` update does not raise: PostgREST rejects the row and the fill is simply never recorded. Nothing else in the pipeline sees it -- `py_compile` accepts any dict, and the unit tests exercise pure decision functions that never touch Supabase
- `scripts/check_schema_columns.py` uses `ast` rather than regular expressions, because these payloads are real Python dict literals and can be parsed properly. 50 payloads and filters checked against `db/schema-columns.json`, a committed snapshot, so the check needs no database credentials. **CORRECTION, 2026-07-29: this entry first said the gate runs in CI. It does not.** This repository has no workflow that runs `test.sh` -- the four here run the bot, not the tests -- so both the schema gate and the 50 unit tests execute only when someone runs `./test.sh` by hand. Mutation testing proved the tests CAN go red; it proved nothing about whether they are ever run. Those are two different properties and one was being counted as both
- **Filters are checked too, not only payloads.** An `sb_update` whose match names a column that does not exist matches nothing and updates nothing. That is the quietest of the three failures: the row stays `claimed`, the purchase happened, and the system never learns of it
- **Mutation-tested, and the first version missed the most important write in the bot.** `claim_row` -- the row that reserves the day's purchase -- is built as a variable and passed afterwards, so a literal-only check never looked at it. Names bound to a dict literal are now resolved, along with keys added later by subscript assignment. A name bound twice is DROPPED rather than merged, since two shapes under one name is exactly where a guess would be wrong. Resolving can only add keys to inspect, never invent one, so it cannot raise a false alarm
- All three mutations now turn it red: a stray column inside `claim_row`, a misspelling in an update payload, and a misspelled filter column
- Regenerate `db/schema-columns.json` after any migration that adds or removes a column; the SQL sits in the script's header

### test: the two decision functions are in the repo and gated
- Until now the only evidence that `cap_decision` and `_repeg_decision` are correct was a pair of ad-hoc scripts run in a chat session and never committed -- the same category as a rotating Actions log, a proof that lasts until the window closes. The Phase 2 verdict leans on the re-peg suite in particular, and its deadline is 08-11
- `tests/test_cap_decision.py` 19 branches / 30 assertions: the DP-4 missing-data paths, both boundaries (price == cap, price == H90), the crash-bounce case the H90 floor exists for, the reason-string format, `cap_params` fallbacks, and a regression pinning the real 07-26 numbers under both the legacy and the rebased rule
- `tests/test_repeg_decision.py` 17 branches / 24 assertions: guard ORDER (`repeg_max` is checked first and must win even when everything else is also wrong), `min_ticks` as configuration rather than a constant, spread collapse, the cap and H90 vetoes on this path, ordermin and zero-volume floors, and that the re-post price is the bid and never the ask
- The earlier "11-branch" and "22-branch" figures described the uncommitted scripts. These suites are rewrites from the source, not those scripts recovered
- **The gate was verified able to go RED**, which is the part usually skipped: three mutations of the live source (invert the H90 condition, loosen `repeg_max` to `>`, re-post at the ask) each turn `test.sh` red. Source restored, diff empty
- Known testability defect, documented in `tests/_harness.py` rather than worked around silently: `kraken_run` reads four credentials at module level, so it cannot be imported without them. The harness stubs the names; the real fix is lazy resolution inside the request signer

### style(messages): en dash instead of em dash
- The two Telegram strings carrying an em dash, both the cap-skip line (`kraken_run.py`, `strike_run.py`). Roberto does not want the em dash in anything he reads on the phone; use `–`

### docs(sync): two false statements corrected in the module docstring
- Both were mine. `kraken_sync.py` still carried the retracted disposal estimate after the hub had been corrected the same day, so code and hub disagreed; and it claimed importing `kraken_run` is side-effect free when that module raises without four credentials. Writing the unit tests is what surfaced the second one

### VALIDATION -- cap telemetry confirmed on its first live day
- 2026-07-28 is the FIRST row carrying `h7` / `h30` / `h90` / `cap_price` together. The 07-27 row is NULL on all four and that is correct, not a regression: v1.4.2 and v1.4.3 were committed at 07:23 and 07:35 CT, and that day's execution had already finished at 06:58 CT. Yesterday's fill ran on the previous code
- The day bought on the maker leg, below both the cap and H90 -- `over_cap` false, `over_h90` false, reconstructed by arithmetic from the row alone, exactly as v1.4.3 intended

### fix(telemetry): the OHLC context is written once, at T0 -- v1.4.4
- **Why**: the first live row failed its own invariant. `cap_price / h7 - 1` read **20.0243%** for a 20% cap. Root cause: `cap_price` and `h90` are written at T0, but `h7` / `h30` were written at FILL time -- and since the maker-first cutover the fill is observed by a later cron cycle, a fresh process, which refetched OHLC. On 07-28 the two fetches sat 10 minutes apart (`execution_started_at` 06:53:19, `ohlc_ts` 07:03:21) and H7 had moved. The stored anchor was not the anchor the cap was computed from
- The decision itself was never affected: `mid`, `cap_price` and `h90` all come from the same T0 fetch, so `skip <=> mid > cap_price AND (guard off OR mid > h90)` held throughout. Only the reconstruction path was wrong, and only through `h7`
- T0 now writes `h7` / `h30` / `ohlc_ts` in the same run that derives `cap_price` from them, placed OUTSIDE the cap branch so `force` runs and reference-less days still carry market context
- `finalize_order()` reads `h7` on the terminal-guard query it already made -- no extra round trip -- and when it is present, reuses the stored context instead of refetching. That also drops one public OHLC call per fill. Rows written before this version still refetch, so nothing regresses for them
- The fill notification now reports the numbers stored on the row, so the message and the row can no longer disagree
- **Telemetry only, cannot alter a decision** -- hence patch
- Validation: 23 hermetic branches driving the REAL `finalize_order` (T0 context reused, payload carries no `h7`/`h30`/`ohlc_ts`, `cap_price / h7 - 1 == cap_pct` to 1e-12, legacy NULL row still refetches and writes, Kraken OHLC outage writes NULL and still fills, Mid present/absent, message shape, terminal row untouched) -- all pass; `test.sh` green

### feat(ux): notifications read as a financial system, not as a log line
- **Why**: Roberto's review. Two messages carried text that only made sense to whoever wrote the code
- **Fill**: pair renders as `KAS/USD`, not `KASUSD` (`pair_label()`, display only -- never fed back to Kraken). Market context becomes its own block, separated from the settlement figures by a blank line: what was paid above, what the market looked like below. Every line drops out when its data is missing -- no `N/A`, no `unknown`, no placeholders
- **`Mid:` now shows the price, not a source label.** It used to print `mid_source`, which has exactly ONE non-NULL value in the entire codebase (`kraken_run.py:717`, hardcoded `"ticker_fallback"`) -- confirmed against the DB: 33 rows `ticker_fallback`, 95 NULL, nothing else. A constant carries no information and read like a leaked variable name. No mid, no line
- **Low balance** rewritten in full Lithuanian with diacritics, bulleted, with the ask and the consequence in a closing sentence. Kept at one decimal (`~4.9 d.`) rather than rounding: the threshold is 5 days, and a message reading `~5 d. (riba – 5 d.)` would look like it contradicts itself
- Emoji unchanged -- one status icon per message, from `ICONS`, no additions
- `strike_run.py` deliberately untouched: it has not executed since 2026-04-25 and its `mid_source` does have a real label. Left for whenever Strike comes back

### feat(sync): mirror Kraken's own account state -- kraken_sync v1.0.0
- **Why**: everything the system knew about the Kraken account was inferred from what the bot itself did, so anything done by hand in the app was invisible. `bf_holdings` only ever accumulates bot buys, so a sell or a withdrawal never reduces it. Measured once the mirror existed: roughly **59,850 KAS was sold across eight trades between 05-26 and 06-30**, plus a 9,486 KAS `spend`, none of it visible anywhere in this database. **RETRACTED, same session:** this paragraph first claimed ~14,865 KAS left around 2026-03-31, reasoning from a note in the crypto-tracker export. That was wrong -- March and April held only buys, and the note was an intent to buy back rather than a disposal. The real disposals are two months later and four times larger
- The manual export cannot close the gap either: it stops 2026-04-03, contains zero KAS sells, and disagrees with `dca_executions` about which bot buys it captured (7 vs 21 in February, 0 vs 17 in March). It is a parallel hand-kept log, not a superset. So there was NO source of truth for the account; Kraken is the only one
- New `src/kraken_sync.py` mirrors **Balance**, **TradesHistory** and **Ledgers** into `kraken_balances` / `kraken_trades` / `kraken_ledgers`, with a per-source watermark in `kraken_sync_state`. Migration `db/v9-kraken-sync.sql`, applied before the push
- **Ledgers is not optional.** A disposal may be a withdrawal rather than a sell, and TradesHistory cannot see a withdrawal. The missing KAS was exactly that ambiguity, so both endpoints ship together or the question stays open
- **READ ONLY, and structurally so**: the module calls three query endpoints and nothing else. It reuses `kraken_run`'s request signing and Supabase helpers -- **CORRECTED, same session: that import is NOT side-effect free.** `kraken_run` reads four credentials with `os.environ[...]` at module level and raises without them, which is why the workflow must map the read-only secret onto those names. The claim of side-effect freedom was mine and it was false -- but the module still shares none of the trading path and runs from its own workflow with its own concurrency group, so a slow or failing sync can never delay a buy window
- Idempotent: trades and ledgers upsert on Kraken's own ids, so re-runs, overlapping windows and future backfills cannot duplicate a row. Each run deliberately rewinds its watermark 30 minutes, because a record landing on the exact second of the previous cutoff could otherwise be missed by both windows -- the overlap is free precisely because of the upsert
- **Permissions are the open question and the code says so.** `Balance` already works (the buy preflight has used it since Phase 1). `TradesHistory` and `Ledgers` additionally need the key's "Query Closed Orders & Trades" and "Query Ledger Entries" boxes, which cannot be checked from here. Each source is attempted independently and records its own status, so one blocked endpoint neither hides the others nor fails the run; the summary prints the exact Kraken setting to tick
- **Its own read-only Kraken key** (Roberto's call, and the better one). `KRAKEN_RO_API_KEY` / `KRAKEN_RO_API_SECRET` are mapped onto the names the shared client already reads, so there is no second signing implementation; the **trading key is not passed to this job at all**. Beyond the obvious containment, two concrete reasons: the permissions here only ever need to GROW, and growing them on the trading key would widen what the trading key can do; and Kraken requires a strictly increasing nonce PER KEY, so a shared key would eventually collide with the DCA workflow's five-minute cadence and make "Invalid nonce" a matter of timing rather than of if. A preflight fails with the exact Kraken and GitHub steps if the secrets are missing, rather than letting an empty key produce a signature error that reads like an outage
- Workflow `kraken_sync.yml` is **manual only** for now. A schedule waits until the first run confirms the permission -- a cron before that would just produce a daily reminder that it is missing, which trains you to ignore a red workflow
- Exit code is judged on recorded STATUS, not row count. A denied source catches its own error and returns zero rows, which is indistinguishable from "nothing new" -- counting rows would have reported a fully blocked run as success. That was a real bug in the first draft, caught by the branch that asserts a total failure exits non-zero
- Validation: 28 hermetic branches driving the real sync functions with stubbed Kraken responses (missing credentials aborting before any Kraken call, pagination across the `count` boundary, watermark sent and rewound on the second run, zero-balance assets excluded, sells captured, a withdrawal captured with a negative amount, permission denial recorded per source without raising, one blocked source not hiding the others, total failure exiting non-zero, `MAX_PAGES` stopping a lying `count`) -- all pass; the 23 v1.5.1, 24 v1.5.0 and 23 v1.4.4 branches still pass; `test.sh` green and now covers `kraken_sync.py`

### feat(telemetry): record the re-peg decision before it is lost -- v1.5.1
- **Why, and why it could not wait**: `_maybe_repeg` computed the decision every inspection cycle and sent it to a `print()`. That lives in a GitHub Actions log which rotates, so **this week's decisions are already gone**. Unlike every other open item, this one does not degrade future precision -- it destroys data that would otherwise exist. Each day without it is an observation lost permanently, which is why it shipped ahead of the acceptance deadline it exists to serve
- **The measurement it rescues.** While re-peg was disabled, "re-peg opportunity" and "fallback day" were the same event, so the fallback count worked as a proxy -- and indeed both historical candidates (07-24 at +1 tick, 07-25 at +3 ticks) ARE the two fallback days. That proxy self-destructs now re-peg is live: from here an opportunity ends in a re-peg and a maker fill, not a fallback. Without this table, 14 days with zero fallbacks could not distinguish "no opportunity arose" from "re-peg quietly consumed them" -- exactly the question the deadline is meant to settle
- **One row per CYCLE, not per decision.** Written on every path through `_maybe_repeg` including all six early returns, so a cycle is never silently absent: the negatives ("rested through five cycles, bid never above the limit") are the frequency measurement. `ticks_above` keeps near-misses visible, which a boolean cannot -- 07-21 and 07-28 both sat at exactly 0.0 ticks, one tick short of firing, invisible in `dca_executions`
- Migration `db/v8-repeg-probe-log.sql` (new table `dca_repeg_log`), applied to Benas AI before the push. No new API calls -- `ticker` and `pair_info` were already fetched on that path. The insert is wrapped: a logging failure can never keep a leg from being re-pegged or a day from being bought
- Telemetry only, decision untouched -- hence patch
- Validation: 23 hermetic branches (fire, near-miss at 0.0 ticks, bid below limit, spread collapse, all six early returns, NULL ticks when market data is unavailable, identity columns, and an exploding insert that must not raise) plus 2 asserting `_repeg_decision` itself is unchanged -- all pass; the 24 v1.5.0 and 23 v1.4.4 branches still pass; `test.sh` green

### CORRECTION -- "opportunities became rarer by construction" was wrong
- Stated earlier the same day that widening `time_window_minutes` 15->30 reduced re-peg opportunities. **It increases them.** Traced in code: `deadline = window_end - CRON_CYCLE_MINUTES` and `_window_bounds_for` gives `target +/- w/2`, so maximum rest is `w - 5` -- 10 min under the old window, 25 min under the new. That is 2.5x more exposure, not less
- The 10-minute figure is confirmed by the data: both fallback days rested exactly 9.9 min. That constant is derived FROM the window, so widening did move it
- Roberto's reasoning, which the code then confirmed: on a day the bid rises, waiting does not help the leg fill -- it only delays the fallback. So widening cannot reduce fallbacks on precisely the days that are re-peg opportunities. Zero fallbacks since 07-26 is the market, not the configuration
- The claim reversed twice in one session. The sequence was intuition -> methodology with a guessed timeout -> verified fact, so the reason it moved is `deadline = window_end - 5`, not indecision

### feat(telemetry): the reference join, four months late -- v1.5.0
- **Why**: `dca-bot-v2.3.md:142` specified it -- nearest `dca_mid_snapshots` row within 180s of the fill, marked `mid_source = 'snapshot'`, else the run's ticker mid as `'ticker_fallback'`. The snapshot side shipped and has been collecting since 2026-02-21 (49,213 rows, 20 pairs). **The join never did.** `finalize_order` hardcoded `'ticker_fallback'` and measured bps against the mid already on the row -- the T0 ticker mid, sampled ~10 minutes BEFORE the fill. Found while checking whether the constant could be dropped from the notification
- The damage is to meaning, not to trading. `impact_bps` is supposed to say "how much worse than the market did we buy"; against a ten-minute-old reference it mostly reported how far the market drifted in those ten minutes
- `resolve_reference_mid()` does the join and returns `(ref_mid, mid_source, ref_mid_ts)`. A lookup failure or an empty window degrades to the old ticker mid -- this is telemetry and must never be able to block a fill
- **Anchored on Kraken's `closetm`**, the instant the order actually closed, NOT `execution_finished_at`, the instant a later cron cycle noticed. Measured over the 33 rows: polling lag averages 17.7s but reaches **151.4s**. The window meant here is the **snapshot join window of this change, +/-180s around the anchor** -- no other window (the cron cycle, the order's time window, the cap reference) is involved -- so 151.4s is 84% of its 180s half-width, spent on our own polling, which would drag the window most of the way off the event it describes. In practice it changes which snapshot is picked on only **1 of 33** rows -- but that row's impact moves by 14.1 bps, so the cheap correctness is worth taking. Falls back to `execution_finished_at` when `closetm` is absent
- **The window is symmetric on purpose, and the spec did not say so.** §142 says "nearest within 180s" without a direction; taking it symmetrically means the reference can be a snapshot up to 180s AFTER the fill, so the metric absorbs a little post-trade drift. The alternative -- backward-only -- was measured and is worse: **6 of 33** rows would lose their reference entirely and fall back to the ~10-minute-old ticker mid, trading a small bias for a large one. As it stands 26 of 33 references precede the fill and 7 follow it, mean distance 17.4s
- New columns `ref_mid` / `ref_mid_ts` (migration `db/v7-reference-mid.sql`). **`mid` is deliberately NOT reused**: it is the T0 ticker mid and it is the cap decision's evidence -- `over_cap` replays as `mid > cap_price`. Overwriting it would silently invalidate every cap replay. **DEPLOY ORDER MATTERS**: these columns are WRITTEN, so migrate before the code ships
- `mid_source` finally carries information, so the notification uses it again -- but as plain words, not the enum: an unqualified `Mid:` line means the market at the fill, `Mid: $x (run ticker)` means no snapshot was close enough and the bps are a weaker claim
- **Not a patch**: this changes what two shipped metrics mean. Minor, though not major -- no buying decision is touched, and the cap path never read these columns
- Validation: 24 hermetic branches driving the REAL `finalize_order` and `resolve_reference_mid` (snapshot beats T0 mid, nearest wins, 180s boundary on both sides, wrong pair excluded, query bounded, no-snapshot fallback labelled, no reference at all leaves bps NULL and still fills, lookup exception degrades, missing `closetm`, `mid` column untouched, both message shapes) -- all pass, plus the 23 v1.4.4 branches as regression; `test.sh` green

### BACKFILL prepared, not applied -- `db/v7-backfill-reference-mid.sql`
- Unlike v6, history here IS honestly recoverable: the snapshots are real observations already stored, not a reconstruction. Verified before writing -- all 33 rows carrying `impact_bps` have both a `closetm` and a snapshot inside the window, so none can be left half-converted
- Mean |delta| 2.0 bps, max 19.6 bps, **6 of 33 rows change sign**. The moves are systematic: the July maker-first rows read as slightly negative impact and become positive. The stale reference was flattering the maker leg, which matters because Phase 2 acceptance is partly judged on it
- Idempotent (`where ref_mid is null`), wrapped in a transaction with a post-condition that aborts on any row left behind, and reversible -- the old value is recomputable as `(avg_price / mid - 1) * 10000` since `mid` is untouched
- **Not applied.** It rewrites reported metrics; that is Roberto's call

### OPEN -- the reference is near the fill, but not at it
- Snapshots are written once per cron cycle (~5 min), so "nearest within 180s" can still be up to 180s away. In the backfill the rows whose snapshot lands within seconds barely move (2026-03-05, -03-06: 1s away, ~0 delta) while the ones 97-148s away move the most -- so some of the remaining number is still reference staleness, not execution quality
- Closing that gap means taking a snapshot AT fill time, which is a decision about Kraken API budget, not a bug fix. Filed

## 2026-07-27 (pirmadienis -- Chicago, session 19)

### feat(telemetry): cap decisions reconstructible from the row -- v1.4.3
- **Why**: closes the OPEN item raised the same day. `dca_executions` stored `h7` / `h30` / `mid` but neither the 90-day floor the guard reads nor the threshold actually applied. A skip row explained itself only in free text; a BUY row said nothing about the cap at all, so "did the H90 guard save this day?" was unanswerable after the fact
- `cap_telemetry()` returns the two columns; the T0 check writes them on **BOTH** outcomes, so a normal buy now carries its cap evidence too
- A stored row replays the decision by arithmetic, no text parsing: `skip <=> mid > cap_price AND (guard off OR mid > h90)`. The mode is inferable as well -- under `ohlc_h7`, `cap_price / h7 = 1 + cap_pct` exactly, which does not hold for the legacy exec-mid reference
- **Scope limit, deliberate**: the columns describe the **T0** decision. The DP-5 fallback re-check and the re-peg guard run in later cycles against a fresher mid; writing their numbers onto the same row would pair a T0 `mid` with a later `cap_price` and read as a contradiction. Those paths stay auditable through their existing enum `reason` markers, which are unchanged
- Migration `db/v6-cap-telemetry.sql` (`h90`, `cap_price`, both nullable, with column comments). **DEPLOY ORDER MATTERS**: unlike v4/v5 these columns are WRITTEN, so the migration must be applied BEFORE the code ships. Applied to Benas AI and verified before push
- Not backfilled: the legacy cap reference averaged rows that have since changed, so historical `cap_price` is not honestly recoverable. Pre-migration rows stay NULL, which reads correctly as "not recorded"
- Telemetry only -- cannot alter a decision, hence patch
- Validation: 10 added branches (threshold arithmetic, missing/zero reference, missing H90, legacy pct) including 3 replay tests that assert a stored row reproduces `cap_decision`'s verdict on the live 07-27 numbers, a crash-bounce and a euphoria day -- 42 cap branches total, all pass; 32 finalize branches still pass; `test.sh` green

### fix(telemetry): market context restored on cross-run fills -- v1.4.2
- **Why**: `h7` / `h30` / `ohlc_ts` have been NULL on every execution since 2026-07-18, and the fill notification lost its `H7 | H30` line. Root cause: `finalize_order()` writes those from `ohlc_ctx`, but of its 8 call sites only the same-run T0 path passed one. After the maker-first cutover a fill is almost always finalized by a LATER cron cycle -- a fresh process -- so the context was gone. Introduced by Step 2 (session 16), surfaced by reviewing the first post-cap live fill
- `finalize_order()` now resolves the pair and lazily calls `get_ohlc_ctx(pair)` when no context was handed in. Cached per run, so the T0 path still does not refetch and the inspection path costs at most one public OHLC call
- Pair resolution prefers the pair encoded in `cl_ord_id` (OUR config string) over Kraken's `descr.pair`, which may return an exchange alias; `descr.pair` stays as fallback. Also removes a latent `None.replace()` on the notification path when neither source resolved
- Fill notification now carries `H7 | H30 | H90` at the same precision as its Price line. H7 is the cap anchor and H90 the guard, so both belong there now that the veto reads them; H30 has no decision weight but the ORDER of the three reads the trend at a glance (`H7 < H30 < H90` = drifting down), which no single number does. `h90` is NOT stored as a column -- see the open item below
- **Telemetry only, cannot alter a decision** -- hence patch. The cap has always fetched its own context in whichever process it runs; it was never affected. Kraken OHLC unavailable still writes NULL and never blocks a fill
- Validation: 32 offline branches driving the REAL `finalize_order` with hermetic stubs (cross-run path, `-fb` and `-r1` leg ids, handed-in context not refetched, empty context, H7-only and H90-only contexts, notification order, unparseable id, no pair at all, exchange alias) -- all pass; the 32 cap/low-balance branches still pass; `test.sh` green

- Raised the "cap decisions are not reconstructible from the DB" gap here; CLOSED the same day by v1.4.3 above

### VALIDATION -- cap rule confirmed on its first live day
- First execution under `cap_mode='ohlc_h7'` BOUGHT, maker leg, at the maker fee rate. The day sat ~3% above H7 but ~7% BELOW H90 -- a cheap day by the 90-day trend
- The legacy rule, recomputed from the same `dca_executions` rows it would have read, put its cap BELOW the day's mid: **07-27 would have been a second consecutive skip, again on a cheap day.** This is the live confirmation of the backtest diagnosis
- The exec-mid bias assumed in the session 18 backtest (ref ~= H7 x 0.990) reproduced exactly on this second independent observation
- Re-peg still NOT proven live: the maker leg filled inside the first inspection cycle at its original limit, so there was no drifting bid to chase. Needs a day that buys but misses the first maker fill

## 2026-07-26 (sekmadienis -- Chicago, session 18)

### feat(ops): low-balance warning -- v1.4.1
- **Why**: a funding gap does not fail loudly. The bot skips the day, and a skipped day is never bought back because there is no carryover. That is exactly how 2026-07-23 was lost (Kraken held $0.49 at the window). Open idea since then; built now
- `warn_if_low_balance()`: after the balance preflight confirms the day is covered, compare the balance against the daily burn and send a Telegram warning when fewer than `low_balance_warn_days` days of buys remain
- **Daily burn = SUM of every enabled order's amount**, not just the order being executed -- computed once in `main()` and passed into `execute_pair`, so the figure stays correct if more orders are added
- **Notification only.** It never changes a trading decision, so unlike the cap and re-peg switches it ships ENABLED (default 5 days). Code default holds even without the migration; the column exists to tune it or switch it off with 0
- Fires at most once per day by construction: the balance preflight sits AFTER the day-unique claim insert, so later runs in the same window return at the 409 before reaching it. Skipped entirely in `dry_run`, and not emitted when the balance already fails the day (that path has its own alert)
- Config in `dca_settings` (migration `db/v5-low-balance-warning.sql`): `low_balance_warn_days` (default 5, 0 = off, CHECK >= 0)
- Validation: 10 added branches (above/at/below threshold, disabled, custom threshold, missing column, zero burn, garbage value, multi-order burn) -- 32 offline branches total, all pass; `test.sh` green

### feat(cap): veto layer rebased onto the H7 daily-close standard -- v1.4.0
- **Why**: the cap reference was `AVG(mid)` over our OWN `dca_executions` from the last 7 days -- ~7 unevenly spaced points that sit on the recent low, so a small uptick read as "above cap". Backtest over 220d of KAS daily closes: the legacy rule skips **~22% of ALL days**, and **62% of those skips happened while the price was below H90** (500d: 28% skipped, 59% of them cheap). Same pattern on 8 other pairs (16-25% skipped, 63-95% of skips cheap), so this was never KAS-specific
- **The rule** (`dca-bot-v2.3.md` Phase 2 weights matrix, H7 veto row + "KAINU DEFINICIJOS" price standard): skip only if `mid > H7 * (1 + cap_pct)` **AND** `mid > H90`, where H7/H90 are SMAs of **Kraken daily closes**, not our own fills
- **Why the H90 leg**: any 7-day mean has a structural blind spot -- straight after a crash a violent bounce reads as far above H7 while still sitting far below H90, i.e. exactly the cheap day an accumulator wants. On 500d of KAS, 3 of the 4 days above `H7 x 1.20` were below H90. The guard adds no tunable number (H90 is already computed every run) and can only PREVENT skips, never cause them
- **Why a near-inert veto is correct**: the aggressive cap did improve average cost per dollar, but without a carryover mechanism a skip is not a saving -- it is capital never deployed (220d: ~29% fewer units accumulated for ~2% better price). Carryover + weight sizing is spec §2, i.e. layer 2-3, not this change
- `cap_decision()`: PURE veto shared by all three call sites (T0 check, DP-5 fallback re-check, re-peg guard) so they can never drift apart. Missing reference or missing H90 = NO skip (DP-4: the day must never end unbought while funds exist)
- `get_cap_context()` routes by mode; `get_ohlc_ctx()` caches the OHLC fetch per run (T0 hands in the context it already built for Phase 1.5 telemetry)
- `_repeg_decision()` now takes `h90` + cap params and calls the same `cap_decision`; dry-run scenario `repeg_cap` re-tuned so its veto still fires under BOTH cap modes
- Config in `dca_settings` (migration `db/v4-cap-h7-ohlc.sql`): `cap_mode` (`exec_7d` default | `ohlc_h7`), `cap_pct` (0.03 default), `cap_require_above_h90` (false default). Code is safe WITHOUT the migration -- missing columns read as legacy. Kill-switch: `cap_mode='exec_7d'`, instant, zero deploy
- Ships with LEGACY defaults: deploying changes nothing until the flip UPDATE is run

### VALIDATION STATUS -- cap rule
- Covered: 22-branch offline test of `cap_decision` / `cap_params` / `get_cap_context` / the `_repeg_decision` cap leg (real 07-26 numbers, the 2025-11-26 crash-bounce shape, exact cap and H90 boundaries, missing-data paths, legacy-mode regression) -- all pass; `test.sh` green
- Covered: 220d and 500d backtests on 9 pairs against Kraken daily closes, driven through the LIVE `ohlc.py` functions
- NOT covered: a live run under `cap_mode='ohlc_h7'`. Backtest price proxy is the daily close (00:00 UTC) while the real buy happens inside the configured execution window, so intraday spikes between closes are invisible and the true veto-zone count could be slightly higher
- The legacy skip rates above model the exec-mid reference as H7 x 0.990 -- the ratio measured on the live 2026-07-26 skip row (ref $0.027961 vs H7 $0.028241). One observation, so treat the exact percentage as approximate; the direction and the "most skips were cheap" finding hold at any plausible bias
- Strike (`strike_run.py`) carries the SAME legacy cap and is NOT touched by this change (dormant: zero enabled orders). BTC is bought via Strike, so this fix does not reach BTC until Strike gets the same treatment

## 2026-07-25 (diena + vakaras -- Chicago; UTC jau 07-26, session 17)

### feat(maker): re-peg (bid-chase) MVP -- v1.3.0 (0e429ec)
- **Why**: the maker limit was STATIC -- posted once at the window-start bid and never re-priced. On 07-24 and 07-25 the KAS bid drifted up/flat, the resting order was left behind the book and never filled, so both days ended in a taker fallback (0.800% instead of 0.400%). Anticipated in the roadmap doc under "if bid-pegging proves too passive"
- `_repeg_decision()`: PURE decision function (no I/O), shared by the live path and the dry-run harness so both exercise identical logic
- `_maybe_repeg()`: live path, hooked into `run_maker_inspection`'s "open, waiting" branch. Deadline / fallback / TTL logic untouched
- Guards: best bid must exceed our resting price by >= `repeg_min_ticks`, must stay maker (`bid < ask`), must be under the same 7D cap the fallback respects, and `repeg_count < repeg_max`
- Scope (MVP): **zero-fill legs only**. A partially filled leg keeps the existing deadline -> fallback path; partial-aware re-peg deferred
- Crash safety (claim-first, preserves the DP-3 no-double-buy discipline): cancel + confirm zero-fill, then park the row as `claimed` / `order_id` NULL / `raw.kraken_cl` = the NEXT client id BEFORE AddOrder. Reconciliation now searches `raw.kraken_cl`, so a crash mid-repeg restores the resting order instead of orphaning it
- Repost rejected by post-only (would cross): the old leg is already canceled zero-fill, so the event resolves straight to a taker fallback for the full budget -- mirrors the initial post-only-reject path
- `raw` on a successful repost nests the Kraken result under `last_result` instead of overwriting -- otherwise `repeg_count` would reset every cycle and the max-count guard would never bind
- Dry-run harness: scenarios 7-9 (`repeg_fill` / `repeg_reject` / `repeg_cap`) appended to SCENARIO_SEQUENCE
- Config in `dca_settings` (migration `db/v3-repeg-mvp.sql`): `repeg_enabled` (default false), `repeg_max`, `repeg_min_ticks`. The code is safe WITHOUT the migration -- missing columns read as defaults, i.e. disabled. Kill-switch: `repeg_enabled=false`, instant, zero deploy
- Shipped default-OFF, then ENABLED live 07-25 evening with a conservative `repeg_max`

### VALIDATION STATUS -- re-peg is NOT yet proven live
- Covered: 11-branch unit test of `_repeg_decision` (trigger, min_ticks, spread collapse, cap incl. exact boundary, max count, ordermin, no-history) -- all pass; the 3 new scenarios driven through the real `_inspect_dry_limit` with hermetic stubs (no prod DB, no Kraken, no `dry_run` flip, so live buying was never paused) -- all pass; `test.sh` green
- NOT covered: **a real live fill.** 07-26 was skipped by the 7D cap, so re-peg has still never fired against Kraken. First real proof needs a day that BUYS but misses the maker fill. The Phase 2 acceptance verdict must not count it as validated before then
- Deliberate tradeoff: the in-bot dry-run harness writes to prod and needs `dry_run=true`, which would halt real buying -- offline validation was chosen instead

### config: live order window widened (no deploy)
- `dca_orders.time_window_minutes` for the live order 15 -> 30, so the maker leg rests ~25 min instead of ~10 (`deadline = window_end - CRON_CYCLE_MINUTES`; the last cycle stays reserved so a fallback still lands inside the window, per I6). Stopgap for the static-limit problem, not a fix

### KNOWN GAP -- 7D cap mis-fires in the current regime (2026-07-26)
- The cap's reference is a 7-day mean, which currently sits on the recent low, so a small uptick reads as "above cap" and skips a buy that is cheap against the 30d / 90d / all-history mean and far below cost basis
- Constraint on the fix: a static price anchor is wrong -- it would stop buying if the market recovers. The anchor must be trend-relative
- Agreed follow-up (not started): (1) recurring daily OHLC append so `dca_ohlc_history` becomes a live uniform daily series -- today it is a ONE-TIME backfill by design (run 07-17 to fill the no-purchase gap), static, not broken; (2) cap rebased onto a long-horizon anchor, skipping only on genuine euphoria. `dca_mid_snapshots` is live and unbroken since February but is recency-weighted, so it needs daily resampling to be an honest long-horizon input

## 2026-07-19 (session 16 tesinys)

### fix: dry run must not depend on real funds (258e7a7)
- The balance preflight gated dry runs on REAL Kraken funds, which killed the first Step 3 evidence attempt; in dry mode the check is now informational only

### Step 4 -- LIVE CUTOVER to maker-first
- `order_strategy='maker_first'`, `dry_run=false` (Roberto GO). First live maker fill 07-20. Rollback = one UPDATE back to `'market'`

## 2026-07-18 (session 16)

### feat: Phase 2 Step 2 -- maker-first execution, limit + market fallback -- v1.2.0 (62e4519)
- Post-only limit leg at best bid (`oflags` incl. `post`), cross-run open-order inspection (the cron cadence IS the timer), cancel -> confirm -> readback before any fallback, fallback sized by the REMAINING quote budget so the per-event money invariant `cost + fee <= budget` holds by construction
- Deployed DORMANT in strict order: migration first, then code, with `order_strategy='market'` so live behaviour was unchanged until the cutover

### fix: review hardening (070a4ac)
- Reconciliation race re-check (closed -> open -> closed): a limit that filled between the ClosedOrders miss and the OpenOrders miss would otherwise have been marked failed with real money spent
- `finalize_order` terminal guard; reason semantics verified (reason NULL = fallback decision pending, reason set = event terminal)

### test: Step 3 harness (290d829)
- Reviewer-ordered, auto-advancing scenario sequencer: fill100, nofill, partial40, post-only reject, crash-after-cancel, crash-after-fallback-submit. Evidence window passed 6/6 with zero double-buys and the money invariant exact to the cent
