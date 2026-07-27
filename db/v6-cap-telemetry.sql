-- v6 — cap decision telemetry on dca_executions
--
-- WHY: a cap decision could not be reconstructed from the DB alone. A skip row
-- carried its reason only as free text, and a BUY row recorded nothing about
-- the cap at all — so afterwards there was no way to check whether the H90
-- guard was what prevented a skip. h7/h30 were already stored; the two numbers
-- the veto actually reads (the H90 trend floor and the applied cap price) were
-- not.
--
-- With these columns a decision is checkable by arithmetic against the mid
-- already stored on the row:
--     skip  <=>  mid > cap_price  AND  (guard off OR mid > h90)
-- and the mode is inferable too: under 'ohlc_h7', cap_price / h7 = 1 + cap_pct
-- exactly, which does not hold for the legacy exec-mid reference.
--
-- ORDER OF DEPLOY: apply this migration BEFORE pushing dca-bot v1.4.3.
-- Unlike v4/v5 these columns are WRITTEN, not read, so the bot cannot run
-- against a schema that lacks them.
--
-- NOT backfilled: the legacy cap reference was an average over rows that have
-- since changed, so historical cap_price is not honestly recoverable. Rows
-- before this migration keep NULL, which correctly reads as "not recorded".

alter table dca_executions
  add column if not exists h90       numeric,
  add column if not exists cap_price numeric;

comment on column dca_executions.h90 is
  'SMA of the last 90 Kraken daily closes at decision time. Second leg of the cap veto: a skip also requires mid > h90, so a crash bounce that is far above H7 but still below the 90d trend is bought, not skipped. NULL = no reference was available and no cap check ran.';

comment on column dca_executions.cap_price is
  'The cap threshold actually applied at T0: ref_price * (1 + cap_pct). Under cap_mode=ohlc_h7 the ref is h7 on this same row; under the legacy exec_7d mode it was an average of our own execution mids and is not stored separately. NULL = no cap check ran.';
