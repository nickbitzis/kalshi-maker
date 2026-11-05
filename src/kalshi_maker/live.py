"""Authenticated Kalshi client + the orders the bot WOULD place. Dry run by default.

    uv run kalshi-live --size 100 --max-capital 1000           # print planned orders, no key needed
    uv run kalshi-live --check-auth                            # needs KALSHI_KEY_ID + KALSHI_KEY_PATH

Real order placement (`--place`) is deliberately not wired into a loop yet: it waits for the
paper results and an explicit go-ahead. Auth and endpoints follow docs.kalshi.com (checked 2026-11-02):
- headers KALSHI-ACCESS-KEY / -TIMESTAMP (ms) / -SIGNATURE
- signature = base64(sign(timestamp + METHOD + path_without_query)), path includes /trade-api/v2
- RSA keys: PSS, SHA-256, MGF1-SHA256, salt = 32; Ed25519 keys: plain signature
- V2 orders: POST /portfolio/events/orders, side "bid" (buy YES) / "ask" (sell YES = buy NO),
  price in YES dollars as a string, count as a string.
"""

from __future__ import annotations

import argparse
import base64
import os
import time
import uuid
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

from .api import BASE
from .scan import scan

PREFIX = urlsplit(BASE).path  # /trade-api/v2


class AuthKalshi:
    def __init__(self, key_id: str, key_pem: bytes, base: str = BASE):
        self.key_id = key_id
        self.key = serialization.load_pem_private_key(key_pem, password=None)
        self.http = httpx.Client(base_url=base, timeout=30)

    @classmethod
    def from_env(cls) -> "AuthKalshi":
        key_id, path = os.environ.get("KALSHI_KEY_ID"), os.environ.get("KALSHI_KEY_PATH")
        if not key_id or not path:
            raise SystemExit("Set KALSHI_KEY_ID and KALSHI_KEY_PATH (see README).")
        return cls(key_id, open(os.path.expanduser(path), "rb").read())

    def sign(self, ts: str, method: str, path: str) -> str:
        msg = (ts + method.upper() + PREFIX + path.split("?")[0]).encode()
        if isinstance(self.key, rsa.RSAPrivateKey):
            sig = self.key.sign(
                msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32), hashes.SHA256()
            )
        elif isinstance(self.key, ed25519.Ed25519PrivateKey):
            sig = self.key.sign(msg)
        else:
            raise ValueError("unsupported key type")
        return base64.b64encode(sig).decode()

    def request(self, method: str, path: str, **kw) -> dict:
        ts = str(int(time.time() * 1000))
        headers = {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": self.sign(ts, method, path),
        }
        r = self.http.request(method, path, headers=headers, **kw)
        if r.is_error:  # keep Kalshi's reason (e.g. which rule a 409 broke) in the error
            raise httpx.HTTPStatusError(f"{r.status_code} {method} {path}: {r.text[:300]}", request=r.request, response=r)
        return r.json() if r.content else {}

    def balance(self) -> dict:
        return self.request("GET", "/portfolio/balance")

    def positions(self) -> dict:
        return self.request("GET", "/portfolio/positions")

    def balance_cents(self) -> int:
        b = self.balance()
        return int(b["balance"]) + int(b.get("portfolio_value", 0))

    def _paged(self, path: str, key: str, params: dict) -> list[dict]:
        out, cursor = [], None
        while True:
            p = dict(params, **({"cursor": cursor} if cursor else {}))
            d = self.request("GET", path, params=p)
            out += d.get(key) or []
            cursor = d.get("cursor")
            if not cursor or not d.get(key):
                return out

    def resting_orders(self) -> list[dict]:
        return self._paged("/portfolio/orders", "orders", {"status": "resting", "limit": 1000})

    def market_positions(self) -> list[dict]:
        return self._paged("/portfolio/positions", "market_positions", {"limit": 1000})

    def cancel_all(self) -> None:
        self.request("DELETE", "/portfolio/events/orders")

    def create_order(self, ticker: str, side: str, yes_price: float, count: float) -> dict:
        return self.request("POST", "/portfolio/events/orders", json=order_body(ticker, side, yes_price, count))

    def cancel(self, order_id: str, ticker: str) -> dict:
        return self.request("DELETE", f"/portfolio/events/orders/{order_id}", params={"market_ticker": ticker})


def order_body(ticker: str, side: str, yes_price: float, count: float) -> dict:
    """Resting, post-only (never takes liquidity), good-till-canceled limit order."""
    assert side in ("bid", "ask")
    return {
        "ticker": ticker,
        "side": side,
        "price": f"{yes_price:.4f}",
        "count": f"{count:.2f}",
        "time_in_force": "good_till_canceled",
        "post_only": True,
        "self_trade_prevention_type": "maker",
        "client_order_id": str(uuid.uuid4()),
    }


def planned_orders(size: float, max_capital: float, n: int) -> list[dict]:
    rows, _ = scan(size=size)
    out, cap = [], 0.0
    for r in rows:
        if min(r["yes_bid"], r["no_bid"]) < 0.05 or r["exp_per_day"] < 1 or cap + r["capital"] > max_capital:
            continue
        cap += r["capital"]
        # yes bid at yes_bid; no bid at no_bid == sell YES ("ask") at 1 - no_bid
        out.append({**order_body(r["ticker"], "bid", r["yes_bid"], size), "_exp_per_day": r["exp_per_day"]})
        out.append({**order_body(r["ticker"], "ask", round(1 - r["no_bid"], 2), size), "_exp_per_day": 0})
        if len(out) >= 2 * n:
            break
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--size", type=float, default=100)
    ap.add_argument("--max-capital", type=float, default=1000)
    ap.add_argument("--markets", type=int, default=15)
    ap.add_argument("--check-auth", action="store_true", help="read balance with your API key")
    a = ap.parse_args()
    if a.check_auth:
        k = AuthKalshi.from_env()
        print("balance:", k.balance())
        return
    orders = planned_orders(a.size, a.max_capital, a.markets)
    exp = sum(o["_exp_per_day"] for o in orders)
    print(f"DRY RUN: {len(orders)} post-only orders, expected ~${exp:.2f}/day in rewards if the books hold")
    for o in orders:
        print(f"  {o['side']:3} {o['ticker']:40} {o['count']:>8} @ {o['price']}")


if __name__ == "__main__":
    main()
