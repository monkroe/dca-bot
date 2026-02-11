# Kraken DCA Bot v1 🤖

Automated Dollar-Cost Averaging via **Kraken API** + **GitHub Actions** + **Supabase**.

## Architecture

```
GitHub Actions (cron 5min)
    │
    ├── Check Chicago time window (07:55–08:10)
    ├── Supabase: read config + claim idempotency
    ├── Kraken: balance check → snapshot → market buy
    ├── Supabase: log execution + fill details
    └── Telegram: alert on fail/skip, weekly summary
```

## Setup

### 1. Supabase

Run `supabase/001_dca_schema.sql` in SQL Editor.

### 2. Kraken API Key

Create at [kraken.com/u/security/api](https://www.kraken.com/u/security/api):
- ✅ Query Funds
- ✅ Create & Modify Orders
- ✅ Query Open Orders & Trades / Query Closed Orders & Trades
- ❌ **NO withdrawal permissions!**

### 3. Telegram Bot (optional)

1. Message [@BotFather](https://t.me/BotFather) → `/newbot`
2. Get bot token
3. Send a message to your bot, then get chat_id:
   `https://api.telegram.org/bot<TOKEN>/getUpdates`

### 4. GitHub Secrets

In repo → Settings → Secrets → Actions:

| Secret | Value |
|--------|-------|
| `KRAKEN_API_KEY` | Your Kraken API key |
| `KRAKEN_API_SECRET` | Your Kraken API secret |
| `SUPABASE_URL` | `https://xxx.supabase.co` |
| `SUPABASE_SERVICE_ROLE_KEY` | From Supabase → Settings → API |
| `TG_BOT_TOKEN` | Telegram bot token |
| `TG_CHAT_ID` | Your Telegram chat ID |

### 5. Enable

Default: `dry_run = true`. Full pipeline runs but no real orders.

Go live:
```sql
UPDATE dca_settings SET dry_run = false WHERE id = 1;
```

## Commands

```bash
# Normal run (GH Actions handles this automatically)
python src/dca_run.py

# Reconciliation only
python src/dca_run.py --reconcile

# Weekly summary only
python src/dca_run.py --weekly
```

Manual trigger: Actions tab → DCA Bot → Run workflow

## Pair Management

```sql
-- Enable BTC
UPDATE dca_orders SET enabled = true WHERE pair = 'XBTUSD';

-- Change KAS amount
UPDATE dca_orders SET base_quote_amount = 15.00 WHERE pair = 'KASUSD';

-- Add new pair
INSERT INTO dca_orders (pair, enabled, base_quote_amount, priority)
VALUES ('SOLUSD', true, 5.00, 3);

-- Disable everything
UPDATE dca_orders SET enabled = false;
```

## Analytics

```sql
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
```

## Status Lifecycle

```
claimed → placed → filled         (happy path)
claimed → skipped_*               (preflight fail)
claimed → failed_kraken           (API error)
claimed → failed_reconciliation   (orphaned claim)
placed  → filled                  (via reconciliation)
```

## Zero Dependencies

Python script uses only stdlib (`urllib`, `hashlib`, `hmac`, `json`, `zoneinfo`).
No `pip install` needed. Runs on any Python 3.9+.
