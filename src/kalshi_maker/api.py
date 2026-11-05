"""Read-only Kalshi REST client (public endpoints, no API key needed)."""

from __future__ import annotations

import time
from datetime import datetime

import httpx

BASE = "https://api.elections.kalshi.com/trade-api/v2"


def parse_time(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


class Kalshi:
    def __init__(self, base: str = BASE, min_interval: float = 0.06):
        # 0.06 s between calls stays under the Basic read budget (20 req/s).
        self.http = httpx.Client(base_url=base, timeout=30)
        self.min_interval = min_interval
        self._last = 0.0

    def get(self, path: str, params=None) -> dict:
        for attempt in range(6):
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            r = self.http.get(path, params=params)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(0.5 * 2**attempt)
                continue
            r.raise_for_status()
            return r.json()
        r.raise_for_status()
        return r.json()

    def _paged(self, path: str, key: str, params: dict) -> list[dict]:
        out, cursor = [], None
        while True:
            p = dict(params)
            if cursor:
                p["cursor"] = cursor
            d = self.get(path, p)
            out += d.get(key) or []
            cursor = d.get("next_cursor") or d.get("cursor")
            if not cursor or not d.get(key):
                return out

    def incentives(self, status: str = "active", type_: str = "liquidity") -> list[dict]:
        return self._paged(
            "/incentive_programs", "incentive_programs",
            {"status": status, "type": type_, "limit": 10000},
        )

    def trading_active(self) -> bool:
        """False during Kalshi's closures (e.g. weekly maintenance, Thu 3-5 AM ET)."""
        return bool(self.get("/exchange/status").get("trading_active"))

    def market(self, ticker: str) -> dict:
        return self.get(f"/markets/{ticker}")["market"]

    def orderbooks(self, tickers: list[str], chunk: int = 100) -> dict[str, dict]:
        """ticker -> {"yes": [(price, size)...], "no": [...]}, ascending by price."""
        out = {}
        for i in range(0, len(tickers), chunk):
            part = tickers[i : i + chunk]
            d = self.get("/markets/orderbooks", [("tickers", t) for t in part])
            for ob in d.get("orderbooks", []):
                out[ob["ticker"]] = parse_book(ob["orderbook_fp"])
        return out

    def trades(self, ticker: str, min_ts: int | None = None, limit: int = 1000) -> list[dict]:
        p = {"ticker": ticker, "limit": limit}
        if min_ts:
            p["min_ts"] = min_ts
        return self.get("/markets/trades", p).get("trades", [])


def parse_book(fp: dict) -> dict:
    side = lambda rows: [(float(p), float(s)) for p, s in (rows or [])]
    return {"yes": side(fp.get("yes_dollars")), "no": side(fp.get("no_dollars"))}
