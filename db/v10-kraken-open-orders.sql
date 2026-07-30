-- v10 -- the fourth mirror source: orders that are still resting
--
-- WHY, and it is a specific failure rather than a completeness urge. On
-- 2026-07-30 the maker leg was refused with EOrder:Insufficient funds while the
-- Kraken USD balance was, on paper, more than the order needed. The money was
-- committed to a resting order -- and nothing in this database could say that,
-- because the mirror covered Balance, TradesHistory and Ledgers, all of which
-- describe money that has ALREADY moved. An open order is money that has not
-- moved and cannot be spent, which is exactly the state that caused the
-- refusal, and it was the one state with no record anywhere.
--
--   kraken_balances     what I hold
--   kraken_trades       what was bought and sold
--   kraken_ledgers      what moved
--   kraken_open_orders  what is SPOKEN FOR  <- this file
--
-- APPEND-ONLY SNAPSHOTS, like kraken_balances and for the same reason: the
-- question is never only "what is resting now" but "what was resting at 06:53",
-- and a table that overwrites itself can never answer the second one. An order
-- that fills or is cancelled simply stops appearing in later snapshots.
--
-- USD locked by a resting buy is derived, not stored:
--     (vol - vol_exec) * price
-- Storing it would freeze one interpretation of the rows into the schema, and
-- a stored number that disagrees with its own inputs is worse than an absent
-- one. `hold_trade` from BalanceEx is the authoritative total; these rows say
-- what makes it up.
--
-- READ ONLY, same as the rest of the mirror: src/kraken_sync.py calls
-- OpenOrders and nothing else. The trading path calls the same endpoint
-- already, so no new key permission is involved.
--
-- IDEMPOTENT: unique on (snapshot_ts, order_txid), so a re-run inside the same
-- snapshot cannot duplicate a row.

create table if not exists public.kraken_open_orders (
  id            bigserial primary key,
  user_id       uuid,
  snapshot_ts   timestamptz not null,
  order_txid    text        not null,
  cl_ord_id     text,
  status        text,
  opened_at_utc timestamptz,
  pair          text,
  side          text,
  ordertype     text,
  price         numeric,
  vol           numeric,
  vol_exec      numeric,
  cost          numeric,
  fee           numeric,
  oflags        text,
  descr         text,
  raw           jsonb,
  synced_at     timestamptz not null default now(),
  unique (snapshot_ts, order_txid)
);

create index if not exists kraken_open_orders_ts_idx   on public.kraken_open_orders (snapshot_ts desc);
create index if not exists kraken_open_orders_pair_idx on public.kraken_open_orders (pair);
create index if not exists kraken_open_orders_cl_idx   on public.kraken_open_orders (cl_ord_id);

-- RLS ON with NO policies: that combination DENIES every request that arrives
-- with a user role. The bot writes as service_role, which bypasses RLS through
-- rolbypassrls rather than through a policy, so it is unaffected. This is the
-- same posture the other three mirror tables were put into on 2026-07-29 --
-- stated here explicitly because a table created later without it would be
-- readable by anyone holding the publishable key, and that is precisely the
-- hole that audit closed.
alter table public.kraken_open_orders enable row level security;

comment on table public.kraken_open_orders is
  'Append-only snapshots of resting Kraken orders. Answers "what was my money committed to at time T" -- the question the 2026-07-30 insufficient-funds refusal could not be answered from. USD locked by a resting buy = (vol - vol_exec) * price.';
