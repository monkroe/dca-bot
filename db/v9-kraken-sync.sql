-- v9 — mirror of Kraken's private account state
--
-- WHY: everything we know about the Kraken account is inferred from what the
-- bot itself did. Anything done by hand in the Kraken app is invisible:
--   * `bf_holdings` is a running total of bot BUYS, so a manual sell or a
--     withdrawal never reduces it. On 2026-07-28 it still read a KAS quantity
--     that predates roughly 14,865 KAS leaving the account around 2026-03-31 --
--     a disposal recorded only in a note in Roberto's crypto-tracker export,
--     never as a Sell row anywhere in this database.
--   * the crypto-tracker export cannot fill the gap either: it stops at
--     2026-04-03, contains no KAS sells at all, and disagrees with
--     dca_executions on which bot buys it captured.
-- So there is no source of truth for the account. Kraken is the only one, and
-- these tables are its mirror.
--
-- READ ONLY. The syncing process (src/kraken_sync.py) calls Balance,
-- TradesHistory and Ledgers and nothing else. It shares kraken_run's request
-- signing but none of its trading path, and it runs from its own workflow so a
-- sync failure can never interfere with a buy.
--
-- WHY THREE TABLES, not one:
--   kraken_balances  answers "what do I hold right now" -- the number to
--                    reconcile bf_holdings against. Append-only snapshots, so
--                    drift is visible over time rather than overwritten.
--   kraken_trades    answers "what was bought and sold", including by hand.
--                    This is what makes sells visible for the first time.
--   kraken_ledgers   answers "what moved", which trades alone cannot: a
--                    withdrawal to a cold wallet is not a trade. The 14,865 KAS
--                    may be either a sell or a transfer -- only Ledgers covers
--                    both cases, which is why it is here and not deferred.
--
-- PERMISSIONS, and the one thing that may block this: the API key already
-- works for Balance (used by the buy preflight since Phase 1) and for order
-- queries. TradesHistory and Ledgers additionally require the key's
-- "Query Ledger & Trade History" permission. Whether that box is ticked cannot
-- be determined from here -- the sync reports a permission error per source and
-- carries on rather than failing the run, so the first run tells us.
--
-- IDEMPOTENT: trades and ledgers are keyed on Kraken's own ids and upserted, so
-- re-running, overlapping windows and backfills cannot duplicate a row.
--
-- ORDER OF DEPLOY: apply before running src/kraken_sync.py -- it writes here.

-- ── current holdings, as Kraken sees them ────────────────────────────
create table if not exists kraken_balances (
  id           bigserial primary key,
  user_id      uuid,
  snapshot_ts  timestamptz not null default now(),
  asset        text        not null,
  balance      numeric     not null
);

create index if not exists kraken_balances_ts_idx    on kraken_balances (snapshot_ts desc);
create index if not exists kraken_balances_asset_idx on kraken_balances (asset, snapshot_ts desc);

comment on table kraken_balances is
  'Append-only snapshots of Kraken''s own Balance response. The authority to reconcile bf_holdings against, since bf_holdings only ever accumulates bot buys and cannot see a sell or a withdrawal. Kept as history rather than a single row so drift between the two is visible over time.';

-- ── every trade, including the ones made by hand ─────────────────────
create table if not exists kraken_trades (
  trade_id    text primary key,
  user_id     uuid,
  order_txid  text,
  pair        text,
  time_utc    timestamptz not null,
  side        text,
  ordertype   text,
  price       numeric,
  cost        numeric,
  fee         numeric,
  vol         numeric,
  margin      numeric,
  raw         jsonb,
  synced_at   timestamptz not null default now()
);

create index if not exists kraken_trades_time_idx on kraken_trades (time_utc desc);
create index if not exists kraken_trades_pair_idx on kraken_trades (pair, time_utc desc);
create index if not exists kraken_trades_side_idx on kraken_trades (side, time_utc desc);

comment on table kraken_trades is
  'Kraken TradesHistory, keyed on Kraken''s trade id. Includes trades made manually in the Kraken app, which no other table in this database has ever contained. `side` is Kraken''s `type` (buy/sell), renamed because `type` collides with too much.';

-- ── every movement, which trades alone do not cover ──────────────────
create table if not exists kraken_ledgers (
  ledger_id   text primary key,
  user_id     uuid,
  refid       text,
  time_utc    timestamptz not null,
  type        text,
  subtype     text,
  asset       text,
  amount      numeric,
  fee         numeric,
  balance     numeric,
  raw         jsonb,
  synced_at   timestamptz not null default now()
);

create index if not exists kraken_ledgers_time_idx  on kraken_ledgers (time_utc desc);
create index if not exists kraken_ledgers_asset_idx on kraken_ledgers (asset, time_utc desc);
create index if not exists kraken_ledgers_type_idx  on kraken_ledgers (type, time_utc desc);

comment on table kraken_ledgers is
  'Kraken Ledgers, keyed on Kraken''s ledger id. Covers what TradesHistory cannot: deposits, withdrawals, transfers and staking movements. Needed because a disposal may be a withdrawal rather than a sell -- the ~14,865 KAS that left the account around 2026-03-31 is exactly that ambiguity.';

-- ── watermark, so each run only asks for what is new ─────────────────
create table if not exists kraken_sync_state (
  source         text primary key,
  last_time_utc  timestamptz,
  last_run_at    timestamptz,
  last_status    text,
  detail         text,
  rows_seen      bigint default 0
);

comment on table kraken_sync_state is
  'One row per source (trades, ledgers, balances). last_time_utc is the watermark the next run resumes from, deliberately rewound a little on each run so a boundary row cannot fall between two windows -- upserts on Kraken''s ids make the overlap free. last_status records permission errors per source, so one blocked endpoint does not hide the others.';
