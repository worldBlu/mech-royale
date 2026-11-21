r"""Sanity probe: does ``--house-vision vision`` actually gate the house bots?

Runs a passive player (an ``act()`` that always returns ``{}``) that stands still in the
open, once under the legacy ``pings`` sensing model and once under ``vision`` (shared line
of sight + short team memory).  If vision is a real gate, the standing target must survive
noticeably longer without the omniscient ping track -- and the per-match ``sight``/``ping``
counters must show the difference.

Usage (from the workspace root):

    & $py tools\sim\probe_house_vision.py --matches 8 --seed 1

This is a diagnostic, not part of the scored 48-match eval; it only uses stdlib and the
sim modules already under tools/sim/.
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.sim import kitcfg  # noqa: E402
from tools.sim.match import Match  # noqa: E402


class StillBot:
    """A player that never moves, aims or fires: a pure house-sensing target."""

    name = "still"

    def __init__(self, hello):
        self.hello = hello

    def act(self, obs):
        return {}


def run_cell(mode, seeds, duration):
    rows = []
    for seed in seeds:
        hello = kitcfg.make_hello(seed, map_dir=kitcfg.MAP_DIR)
        match = Match(StillBot(hello), hello, seed, duration=duration,
                      house_vision=mode, collect_events=False)
        r = match.run()
        rows.append(r)
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description="House-sensing sanity probe (standing target)")
    ap.add_argument("--matches", type=int, default=8, help="matches per sensing mode")
    ap.add_argument("--seed", type=int, default=1, help="base seed")
    ap.add_argument("--duration", type=float, default=480.0, help="match duration cap (s)")
    args = ap.parse_args(argv)
    if args.matches < 1:
        ap.error("--matches must be >= 1")

    seeds = [args.seed + i * 10007 for i in range(args.matches)]
    cells = {mode: run_cell(mode, seeds, args.duration) for mode in ("pings", "vision")}

    print("standing-target probe (player never moves): %d matches, seed %d, cap %.0f s"
          % (args.matches, args.seed, args.duration))
    print("%-6s %-6s %6s %6s %8s %8s %6s %5s %6s"
          % ("mode", "match", "seed", "place", "t_s", "hp_end", "sight", "ever", "pings"))
    for mode in ("pings", "vision"):
        for i, r in enumerate(cells[mode], 1):
            print("%-6s %-6d %6d %6d %8.1f %8.1f %6.2f %5d %6d"
                  % (mode, i, r["seed"], r["place"], r["t"], r["hp_end"],
                     r["house_sight_avg"], r["house_sight_ever"], r["house_pings"]))
    print("")
    for mode in ("pings", "vision"):
        rs = cells[mode]
        n = len(rs)
        surv = sorted(r["t"] for r in rs)
        print("%-6s mean survival %6.1f s (median %6.1f, max %6.1f), mean sight %.2f houses, "
              "mean pings %5.1f, mean place %.2f"
              % (mode, sum(surv) / n, surv[n // 2], surv[-1],
                 sum(r["house_sight_avg"] for r in rs) / n,
                 sum(r["house_pings"] for r in rs) / n,
                 sum(r["place"] for r in rs) / n))
    return 0


if __name__ == "__main__":
    sys.exit(main())
