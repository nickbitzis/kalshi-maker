"""Summarize a paper run:  uv run kalshi-report [run_id]  (default: latest)."""

from __future__ import annotations

import json
import sys

from .scan import DATA


def summarize(run_dir) -> str:
    cfg = json.loads((run_dir / "config.json").read_text())
    cycles = [json.loads(l) for l in (run_dir / "cycles.jsonl").read_text().splitlines() if l.strip()]
    state = json.loads((run_dir / "state.json").read_text())
    if not cycles:
        return "no cycles yet"
    hrs = (cycles[-1]["t"] - cycles[0]["t"]) / 3600
    last = cycles[-1]
    peak_cap = max(c["resting_capital"] + c["inventory_cost"] for c in cycles)
    per_day = lambda x: x / hrs * 24 if hrs > 0 else 0
    lines = [
        f"Paper run {cfg['started']}: {len(cfg['markets'])} markets, {cfg['size']:g} contracts/side, {hrs:.2f} h",
        f"Rewards accrued   ${last['reward']:.2f}  (~${per_day(last['reward']):.2f}/day)",
        f"Paired-fill P&L   ${last['realized']:.2f}",
        f"Open inventory    ${last['mtm']:+.2f} marked at mid (cost ${last['inventory_cost']:.2f})",
        f"Fees              ${last['fees']:.2f}",
        f"NET               ${last['net']:+.2f}  (~${per_day(last['net']):+.2f}/day)  fills: {last['fills']}",
        f"Peak dollars tied up ${peak_cap:,.0f}",
        "",
        "ticker | reward | realized | mtm | fees | net | fills | valid% | pos_yes | pos_no",
    ]
    for b in sorted(state, key=lambda b: -(b["reward"])):
        mid = b["mid_yes"]
        mtm = (b["pos_yes"] * mid + b["pos_no"] * (1 - mid) if mid is not None else 0) - b["cost_yes"] - b["cost_no"]
        net = b["reward"] + b["realized"] + mtm - b["fees"]
        valid = 100 * b["valid_s"] / b["total_s"] if b["total_s"] else 0
        lines.append(
            f"{b['ticker']} | {b['reward']:.2f} | {b['realized']:.2f} | {mtm:+.2f} | {b['fees']:.2f} | "
            f"{net:+.2f} | {b['fills']} | {valid:.0f} | {b['pos_yes']:g} | {b['pos_no']:g}"
        )
    return "\n".join(lines)


def main():
    runs = sorted((DATA / "paper").iterdir())
    run = next((r for r in runs if r.name == sys.argv[1]), None) if len(sys.argv) > 1 else runs[-1]
    print(summarize(run))


if __name__ == "__main__":
    main()
