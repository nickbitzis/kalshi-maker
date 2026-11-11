# kalshi-maker

A market-making bot for Kalshi's **Liquidity Incentive Program**: Kalshi pays a daily reward pool per market to traders who keep resting orders near the top of the book. The bot posts a YES bid and a NO bid at each side's reward "reference price", collects its share of the pool, and pairs off fills (a YES + NO pair settles at exactly $1). It does not predict outcomes.

Built end to end: reward-model scanner, paper trader on live order books, live bot with real money, and an honest post-mortem.

## Pipeline
| Command | What it does |
|---|---|
| `uv run kalshi-scan --size 250` | Pulls every active reward program and order book (~4,500 markets, ~$450K in pools), models our share of each pool at a given size, ranks by expected $/day per $ of capital. |
| `uv run kalshi-paper --markets 20 --size 250 --hours 10` | Paper-trades on live books: re-quotes at the reference price, accrues rewards per snapshot, simulates fills from the public trade tape with queue position, pairs off YES/NO, marks inventory at mid. |
| `uv run kalshi-report [run]` | P&L breakdown of a paper run: rewards, paired-fill P&L, open inventory, fees, per market. |
| `uv run kalshi-bot` | Live bot. Dry run by default; `--place` sends real post-only orders. |
| `uv run pytest` | 33 tests: reward scoring, fill simulation, pairing, price/inventory guards, request signing, and the live bot against a fake exchange. |

Code in `src/kalshi_maker/`: `api.py` (public REST), `lip.py` (reward math, fees), `scan.py`, `paper.py`, `report.py`, `live.py` (signed client, RSA-PSS / Ed25519), `bot.py` (live loop).

## Live bot safety
- Post-only orders: it can never take liquidity or pay taker fees.
- `--max-capital`: resting orders + held inventory never exceed the cap.
- `--max-loss`: if account value falls this far, cancel everything and stop.
- Kill switch file `data/STOP`; any exit (Ctrl-C, SIGTERM, crash) cancels all resting orders.
- Waits out exchange closures (`/exchange/status`), reconciles its own resting orders each cycle so it never chases itself.
- Never bids a side when pairing it with held inventory would cost ≥ $1; stops quoting outside 5–95¢.

## Results
**Paper (10 h runs, $3K):** ~$310/day modeled rewards; one run lost money on a market that went to near-certain YES, which led to the price and pair-cost guards above.

**Live run 1 (24 h, $300, 25 contracts/side, 10 markets):**
- Rewards paid: **$6.69**, about 1/4 of the paper model. Reasons found: rewards are paid only after each program period closes, a period paying under $1 pays nothing, and real competition was deeper than the static book.
- Fill P&L: about **−$20**. Almost all of it came from news-driven, this-week markets (executive orders, Trump appearances): informed traders hit a resting bid once, then the price collapsed. A slow, two-way market (a Rotten Tomatoes score) made money on 13 round-trip fills.
- Net: about −$11. Maker fees confirmed $0; resting orders do not lock cash.
- 3,579 rejected orders all fell in Kalshi's weekly maintenance window, which led to the closure check.

**Changes for run 2:** skip Politics/Elections categories and markets closing within 7 days, hold one market per event (strikes of one event move together), and skip markets expected to earn under $2 per reward period.

**Live run 2 (72 h, $220 cap, 9 markets):**
- Net about **−$2.69** including rewards; rewards came to roughly $2/day.
- Two-way Rotten Tomatoes markets realized +$5 over 30+ round-trip fills: the strategy works where flow is two-sided.
- One-sided fills on long-dated markets (a subscriber-count metric, an IPO date) lost ~$10 marked to market.
- Found at shutdown: the laptop was asleep at time-up so cancel-all failed, and Kalshi's bulk cancel endpoint returns 200 without cancelling. `cancel_all` now cancels each order, confirms the book is empty, and retries on network errors.

**Verdict:** at retail size the rewards (~$2–7/day) do not cover adverse selection on one-sided fills. Project closed after ~$18 total cost on a $300 test.

## Lessons
- Model the payout rules, not just the scoring rule: payout timing and minimums cut real rewards by ~4x.
- For a passive maker, market selection matters more than quote placement. Adverse selection lives in news-driven markets.
- Paper fills from the trade tape understate adverse selection.

## Setup
```bash
uv sync
uv run kalshi-scan --size 100
export KALSHI_KEY_ID=<key id> KALSHI_KEY_PATH=<path to private key>
uv run kalshi-live --check-auth
uv run kalshi-bot --max-capital 300 --size 25            # dry run
```
Not financial advice. Kalshi can change or end the incentive program at any time.

## License
MIT, see `LICENSE`.
