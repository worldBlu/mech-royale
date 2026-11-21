"""Diagnose *how* the bot dies: wrap ``Bot.act`` and record the last seconds before death.

  python tools/profile_match.py --matches 12

Prints, per death, the final 12 s of (t, mode, hp, visible hostiles, aimed, nearest hostile,
kills, cubes, weapon kinds) and then aggregates the recurring causes.
"""
import argparse
import collections
import json
import math
import os
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tools.sim import kitcfg, loader  # noqa: E402
from tools.sim.match import Match  # noqa: E402


def run_one(Bot, seed, duration=None):
    hello = kitcfg.make_hello(seed, config=kitcfg.base_config(), map_dir=kitcfg.MAP_DIR)
    bot = Bot(hello)
    log = []

    orig = bot.act

    def act(obs):
        me = obs.get("self") or {}
        pos = me.get("pos") or [0, 0, 0]
        host = []
        for a in obs.get("mechs") or []:
            if float(a.get("hp") or 0.0) <= 0.0:
                continue
            dx = a["pos"][0] - pos[0]
            dy = a["pos"][1] - pos[1]
            host.append((math.hypot(dx, dy), a))
        host.sort(key=lambda h: h[0])
        aimed = 0
        for d, a in host:
            fx, fy = a["pos"][0] - pos[0], a["pos"][1] - pos[1]
            d2 = math.hypot(fx, fy)
            if d2 <= 0.0:
                continue
            fa = float((a.get("aim") or [0.0, 0.0])[0])
            diff = (math.atan2(fy, fx) - fa + math.pi) % (2 * math.pi) - math.pi
            if abs(diff) < 0.10 and d2 < 240.0:
                aimed += 1
        log.append({
            "t": float(obs.get("t") or 0.0),
            "mode": getattr(bot.policy, "mode", None),
            "hp": float(me.get("hp") or 0.0),
            "max_hp": float(me.get("max_hp") or 1400.0),
            "near": len(host),
            "near150": sum(1 for d, _ in host if d < 150.0),
            "aimed": aimed,
            "d0": host[0][0] if host else None,
            "cubes": int(me.get("cubes") or 0),
            "weaps": [w.get("kind") for w in (me.get("weapons") or []) if w],
        })
        return orig(obs)

    bot.act = act
    match = Match(bot, hello, seed, duration=duration, trace_every=20)
    res = match.run()
    res["log"] = log
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--matches", type=int, default=12)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--tail", type=float, default=12.0)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    Bot, _ = loader.load_main(cache_world=True)
    deaths = []
    for i in range(args.matches):
        seed = args.seed + i * 10007
        res = run_one(Bot, seed)
        log = res["log"]
        deaths.append((seed, res, log))
        if args.verbose:
            print("seed %d place %d t=%.1f hp=%.0f kills=%d dmg=%d/%d cubes=%d"
                  % (seed, res["place"], res["t"], res["hp_end"], res["kills"],
                     res["damage_dealt"], res["damage_taken"], res["cubes"]))
            cut = res["t"] - args.tail
            for row in log:
                if row["t"] >= cut:
                    print("   t=%5.1f %-6s hp=%6.0f/%4.0f near=%2d/%2d aimed=%d d0=%s cubes=%d %s"
                          % (row["t"], row["mode"], row["hp"], row["max_hp"], row["near"],
                             row["near150"], row["aimed"],
                             "-" if row["d0"] is None else "%.0f" % row["d0"],
                             row["cubes"], ",".join(row["weaps"])))

    # ---- aggregate over deaths
    died = [(s, r, l) for s, r, l in deaths if not r.get("end") == "timeout"]
    print("\n%d matches, %d deaths" % (len(deaths), len(died)))
    if not died:
        return 0

    tail_window = []
    mode_last = collections.Counter()
    lowhp_still = 0
    alone = 0
    for seed, r, log in died:
        last = log[-1]
        mode_last[last["mode"]] += 1
        t0 = r["t"] - 10.0
        win = [row for row in log if row["t"] >= t0]
        tail_window.append(win)
        if any(row["near150"] == 0 for row in win):
            alone += 1
        if last["near150"] >= 2:
            lowhp_still += 1
    print("mode at death:", dict(mode_last))
    print("deaths with a 10 s window containing no hostile within 150 m:", alone)
    print("deaths while 2+ hostiles within 150 m:", lowhp_still)
    hp_at_death = [r["hp_end"] for _, r, _ in died]
    print("hp at death: median %.0f mean %.0f" % (statistics.median(hp_at_death),
                                                  statistics.mean(hp_at_death)))
    # hp fraction 5 s before death vs at death: were we already low or one-shot?
    frac = []
    for seed, r, log in died:
        t0 = r["t"] - 5.0
        win = [row for row in log if row["t"] >= t0]
        if win:
            frac.append(win[0]["hp"] / max(1.0, win[0]["max_hp"]))
    if frac:
        print("hp fraction 5 s before death: median %.2f mean %.2f" % (statistics.median(frac),
                                                                      statistics.mean(frac)))
    # how much damage we eat in the final 5 s
    per5 = []
    for seed, r, log in died:
        t0 = r["t"] - 5.0
        win = [row for row in log if row["t"] >= t0]
        if win:
            per5.append(win[0]["hp"] - r["hp_end"])
    if per5:
        print("hp lost in final 5 s: median %.0f mean %.0f" % (statistics.median(per5),
                                                               statistics.mean(per5)))
    # weapons in hand across matches
    kinds = collections.Counter()
    for _, r, log in deaths:
        seen = set()
        for row in log:
            seen.update(row["weaps"])
        for k in seen:
            kinds[k] += 1
    print("matches where we at some point held:", dict(kinds))
    return 0


if __name__ == "__main__":
    sys.exit(main())
