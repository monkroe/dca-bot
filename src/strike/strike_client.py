import os
import requests
from typing import Optional

STRIKE_API_BASE = "https://api.strike.me/v1"

class StrikeClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        })

    def get_ticker(self, pair: str = "BTCUSD") -> Optional[dict]:
        r = self.session.get(f"{STRIKE_API_BASE}/rates/ticker", timeout=10)
        r.raise_for_status()
        tickers = r.json()
        for t in tickers:
            if t.get("sourceCurrency") == "USD" and t.get("targetCurrency") == "BTC":
                return t
        return None

    def get_balances(self) -> list:
        r = self.session.get(f"{STRIKE_API_BASE}/balances", timeout=10)
        r.raise_for_status()
        return r.json()

    def get_usd_balance(self) -> float:
        for b in self.get_balances():
            if b.get("currency") == "USD":
                return float(b.get("available", {}).get("amount", 0))
        return 0.0

    def create_quote(self, usd_amount: str) -> dict:
        payload = {
            "sourceCurrency": "USD",
            "targetCurrency": "BTC",
            "sourceAmount": {"amount": usd_amount, "currency": "USD"}
        }
        r = self.session.post(f"{STRIKE_API_BASE}/currency-exchange-quotes", json=payload, timeout=10)
        r.raise_for_status()
        return r.json()

    def execute_quote(self, quote_id: str) -> dict:
        r = self.session.patch(
            f"{STRIKE_API_BASE}/currency-exchange-quotes/{quote_id}/execute",
            timeout=10
        )
        r.raise_for_status()
        return r.json()

    def get_quote(self, quote_id: str) -> dict:
        r = self.session.get(f"{STRIKE_API_BASE}/currency-exchange-quotes/{quote_id}", timeout=10)
        r.raise_for_status()
        return r.json()
