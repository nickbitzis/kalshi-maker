# Kalshi Maker

Kalshi Maker is a market-making bot for Kalshi's Liquidity Incentive Program. Kalshi pays a reward pool in each market to traders who keep resting orders near the top of the book. This bot scans every rewarded market, estimates its share of each pool, paper-trades the strategy on live order books, and can run live with strict risk limits.

The strategy doesn't try to predict outcomes. It posts a yes bid and a no bid in each market at the price that earns full reward credit. If both sides fill, it holds a yes and no pair that settles at exactly $1.

## Commands

| Command | What it does |
|---|---|
| `uv run kalshi-scan --size 250` | Pulls every active reward program and order book, computes the expected share at a given size, and ranks markets by expected reward per day per dollar tied up. |
| `uv run kalshi-paper --markets 20 --size 250 --hours 10` | Paper-trades on live books. It re-quotes at the reward price, accrues rewards per snapshot, simulates fills from the real trade tape with queue position respected, and pairs off yes and no inventory. |
| `uv run kalshi-report [run]` | Summarizes a paper run: rewards, paired-fill P&L, open inventory, fees and net, by market. |
| `uv run kalshi-live --check-auth` | Confirms an API key works by reading the account balance. |
| `uv run kalshi-bot` | Live bot. Without `--place` it prints what it would do and sends nothing. With `--place` it sends real post-only orders. |
| `uv run pytest` | 31 tests. |

Scanning and paper trading use only public data, so they need no account.

## Risk controls

- `--max-capital` caps resting orders plus inventory.
- `--max-loss` stops the bot and cancels every order.
- Creating the file `data/STOP` acts as a kill switch, and every exit cancels all open orders.
- It stops quoting a side priced outside 5 to 95 cents, and it never bids a side when pairing with held inventory would cost $1 or more.
- It checks the exchange status and waits during scheduled closures.
- It skips markets that close within 48 hours and markets whose expected reward per period is below the payout minimum.

## Setup for live trading

Create an API key in Kalshi's settings and save the `.pem` file outside the repo. Then:

```bash
export KALSHI_KEY_ID=<key id>
export KALSHI_KEY_PATH=~/.kalshi/<file>.pem
uv run kalshi-live --check-auth
uv run kalshi-bot --max-capital 300 --size 25                          # dry run
uv run kalshi-bot --max-capital 300 --size 25 --max-loss 50 --place    # live
```

## Code

| File | What it does |
|---|---|
| `api.py` | Public REST client |
| `lip.py` | Reward scoring and fee math |
| `scan.py` | Market scan and ranking |
| `paper.py` | Paper trading engine and fill simulation |
| `report.py` | Run reports |
| `live.py` | Signed client for live orders |
| `bot.py` | Live trading loop |

The tests cover reward scoring, fill simulation, pairing, inventory and price guards, request signing, and the live bot against a fake exchange.

## Limits

The expected rewards assume today's competition stays the same, and fills are simulated from public trades. Real results depend on other market makers and on how Kalshi applies its scoring rules. This is a personal project, not investment advice.
