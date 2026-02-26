# dca-bot 🤖

Automated Dollar-Cost Averaging via **GitHub Actions** + **Supabase** + **Telegram**.
Two independent bots — same architecture, different exchanges.

## Bots

| Bot | File | Exchange | Assets | Schedule |
|-----|------|----------|--------|----------|
| Kraken DCA | `src/dca_run.py` | Kraken Pro | KAS, SOL, XBT | Every 5 min (time-window) |
| Strike DCA | `src/strike_run.py` | Strike | BTC | Every 5 min (time-window) |

## Architecture
GitHub Actions (cron every 5 min)
│
├── src/dca_run.py      → Kraken API → Supabase → Telegram
└── src/strike_run.py   → Strike API → Supabase → Telegram
### Execution Flow (both bots)
Reconciliation       — resolve stale claimed/placed executions
Load orders          — read enabled orders from DB
Time window check    — only execute within order's time window
Balance check        — skip if insufficient funds
Ticker snapshot      — record bid/ask/mid for analytics
7D cap check         — skip if price > 7D avg × 1.03
Execute              — place order (Kraken) or quote (Strike)
Finalize             — poll for fill, update DB, notify Telegram
### Smart Pricing (7D Cap)

Skips purchase if current price is more than 3% above the 7-day average.
Prevents buying at local tops. Override with `--force`.

### Idempotency

Each execution has a unique `cl_ord_id`:
- Live:     `dca-KASUSD-2026-02-26-0845`
- Dry run:  `dca-KASUSD-2026-02-26-dry-1740000000000`
- Force:    `dca-KASUSD-2026-02-26-force-1740000000000`

Duplicate runs for the same `cl_ord_id` are rejected (HTTP 409).

### Status Lifecycle
claimed → placed → filled         (happy path)
claimed → skipped_*               (preflight fail)
claimed → failed_kraken           (API error)
claimed → failed_reconciliation   (orphaned claim)
placed  → filled                  (via reconciliation)
### Zero Dependencies

Both scripts use only Python stdlib (`urllib`, `hashlib`, `hmac`, `json`, `zoneinfo`).
No `pip install` needed. Requires Python 3.11+.

---

## Setup

### 1. Supabase

Run `supabase/001_dca_schema.sql` in SQL Editor.

### 2. Kraken API Key

Create at [kraken.com/u/security/api](https://www.kraken.com/u/security/api):
- ✅ Query Funds
- ✅ Create & Modify Orders
- ✅ Query Open Orders & Trades / Query Closed Orders & Trades
- ❌ **NO withdrawal permissions!**

### 3. Strike API Key

Create at [dashboard.strike.me](https://dashboard.strike.me) → API Keys:
- ✅ Read balance
- ✅ Create quotes
- ❌ **NO withdrawal permissions!**

### 4. Telegram Bot (optional)

1. Message [@BotFather](https://t.me/BotFather) → `/newbot`
2. Get bot token
3. Get chat_id: `https://api.telegram.org/bot<TOKEN>/getUpdates`

### 5. GitHub Secrets

Settings → Secrets → Actions:

| Secret | Used by |
|--------|---------|
| `KRAKEN_API_KEY` | Kraken bot |
| `KRAKEN_API_SECRET` | Kraken bot |
| `STRIKE_API_KEY` | Strike bot |
| `SUPABASE_URL` | Both |
| `SUPABASE_SERVICE_ROLE_KEY` | Both |
| `TG_BOT_TOKEN` | Both |
| `TG_CHAT_ID` | Both |

### 6. Go Live

Both bots default to `dry_run = true`. Full pipeline runs but no real orders.

```sql
-- Kraken
UPDATE dca_settings SET dry_run = false WHERE id = 1;

-- Strike
UPDATE strike_dca_settings SET dry_run = false WHERE id = 1;
Database Tables
Kraken
Table
Purpose
dca_settings
Global settings (fee rate, dry_run, time window)
dca_orders
Per-pair orders (amount, time, priority)
dca_executions
Execution log (fill details, status, raw response)
dca_mid_snapshots
Price snapshots for analytics
dca_commands
SQL control-plane (change settings via Telegram)
dca_notifications
Sent notification log (weekly summary dedup)
Strike
Table
Purpose
strike_dca_settings
Settings (dry_run, time window, user_id)
strike_dca_orders
Per-pair orders
strike_dca_executions
Execution log
GitHub Actions
Workflow
File
Trigger
Kraken DCA Bot
.github/workflows/dca_bot.yml
Every 5 min + manual
Strike DCA Bot
.github/workflows/strike_dca.yml
Every 5 min + manual
Manual Modes
Mode
Description
(empty)
Normal scheduled run
--force
Bypass time window — buy immediately
--reconcile
Reconciliation only — no new orders
--weekly
Weekly summary only (Kraken)
Pair Management (Kraken)
-- Enable BTC
UPDATE dca_orders SET enabled = true WHERE pair = 'XBTUSD';

-- Change KAS amount
UPDATE dca_orders SET base_quote_amount = 15.00 WHERE pair = 'KASUSD';

-- Add new pair
INSERT INTO dca_orders (pair, enabled, base_quote_amount, priority)
VALUES ('SOLUSD', true, 5.00, 3);

-- Disable everything
UPDATE dca_orders SET enabled = false;
Analytics (Kraken)
-- Today's executions
SELECT * FROM v_dca_daily WHERE trade_date = CURRENT_DATE;

-- This week's summary
SELECT * FROM v_dca_weekly ORDER BY week_start DESC LIMIT 1;

-- All failures
SELECT * FROM v_dca_failures;

-- All-time stats per pair
SELECT
  pair,
  COUNT(*) FILTER (WHERE status = 'filled') AS fills,
  SUM(filled_quote_cost + COALESCE(fee_quote, 0)) AS total_invested,
  SUM(filled_base_volume) AS total_volume,
  SUM(filled_quote_cost + COALESCE(fee_quote, 0))
    / NULLIF(SUM(filled_base_volume), 0) AS avg_effective_price
FROM dca_executions
GROUP BY pair;
Environment Variables
Variable
Default
Description
DCA_USD_SAFETY_MARGIN
0 (Kraken) / 0.03 (Strike)
USD buffer to avoid overspend
TG_NOTIFY_ON_FILL
true
Send Telegram on successful fill
DCA_COMMANDS_MAX_PER_RUN
25
Max SQL commands per run (Kraken)
Local Testing
./test.sh    # Python syntax check for all scripts
