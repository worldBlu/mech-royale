"""Evaluate the Mech Royale bot over simulated matches.

Usage (from the workspace root)::

    python tools/eval.py                          # 8 matches, current bot
    python tools/eval.py --matches 48             # the scoreboard-size run
    python tools/eval.py --quick                  # small, short matches for iteration
    python tools/eval.py --bot example            # the kit's reference bot
    python tools/eval.py --knob vision.range=120 --knob weapons.gun.damage=30
    python tools/eval.py --variant no-fire        # deliberate policy/config A/B variants

Each match runs in-process: the bot is constructed exactly as ``mf.run`` would construct it
(``Bot(hello)`` with a hello carrying ``config`` and the map directory), then driven through
protocol observations every ``decide_every`` ticks.  The simulator is deterministic for a
given seed, so comparing two knobs uses exactly the same spawns, zone, loot and house AI.

Score = mean(16 - place).  ``place`` is the number of mechs alive when the bot dies (first
death = 16th, last survivor = 1st); on the 480 s timeout the survivors are ranked by HP.
"""
import argparse
import copy
import json
import os
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tools.sim import kitcfg, loader  # noqa: E402
from tools.sim.house import LEVEL_COUNTS  # noqa: E402
from tools.sim.match import Match  # noqa: E402

VARIANTS = ("", "no-fire", "no-boost", "no-loot", "no-farm")


def _parse_literal(text):
    return kitcfg.parse_value(text)


def _patch_variant(bot, variant):
    """Apply a deliberately degraded policy/config variant (for A/B evidence)."""
    if not variant:
        return
    if variant == "no-fire":
        return  # handled by Match(no_fire=True): the bot still aims but never shoots
    if variant == "no-boost":
        act = bot.act
        def act_no_boost(obs):
            a = dict(act(obs) or {})
            a["boost"] = False
            return a
        bot.act = act_no_boost
        return
    if variant == "no-loot":
        if hasattr(bot, "policy"):
            bot.policy._pickup_step = lambda *a, **k: (None, None)
            bot.policy._cube_step = lambda *a, **k: None
        return
    if variant == "no-farm":
        if hasattr(bot, "policy"):
            bot.policy._npc_step = lambda *a, **k: (1e9, None)
            bot.policy._crate_step = lambda *a, **k: None
        return
    raise ValueError("unknown variant: %s" % variant)


def _make_bot_factory(kind):
    if kind == "main":
        Bot, mod = loader.load_main(cache_world=True)
        return lambda hello: Bot(hello)
    if kind == "example":
        return loader.make_example_bot_factory()
    raise ValueError("unknown bot kind: %s" % kind)


def _fmt_match(i, r):
    return (
        "#%02d seed=%-6d place=%2d score=%5.2f t=%5.1fs end=%-5s hp=%7.1f "
        "kills=%2d dmg=%7.1f/%7.1f zone=%6.1f out=%5.1fs cubes=%2d act=%5.2fms "
        "sight=%.2f/%.0f ever=%2d ping=%2d fail=%d"
        % (
            i + 1, r["seed"], r["place"], r["score"], r["t"], r["end"], r["hp_end"],
            r["kills"], r["damage_dealt"], r["damage_taken"], r["zone_damage"],
            r["time_outside_zone"], r["cubes"], r["act_ms_mean"],
            r["house_sight_avg"], r["house_sight_max"], r["house_sight_ever"],
            r["house_pings"], r.get("bot_fails", 0),
        )
    )


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--matches", type=int, default=8, help="number of matches (default 8)")
    ap.add_argument("--seed", type=int, default=1, help="base seed (default 1)")
    ap.add_argument("--bot", choices=("main", "example"), default="main")
    ap.add_argument("--house-vision", choices=("pings", "vision"), default="pings",
                    help="house sensing model: 'pings' (noisy shared hunt track, legacy) or "
                         "'vision' (only shared line-of-sight + 3 s team memory)")
    ap.add_argument("--knob", action="append", default=[],
                    help="config override, dotted path: vision.range=120")
    ap.add_argument("--variant", choices=VARIANTS, default="",
                    help="deliberate A/B policy variant")
    ap.add_argument("--quick", action="store_true",
                    help="short matches (150 s cap) and at most 4 matches when unspecified")
    ap.add_argument("--duration", type=float, default=None, help="override match duration (s)")
    ap.add_argument("--quiet", action="store_true", help="only print the summary")
    ap.add_argument("--json-out", default=None, help="write full results as JSON")
    ap.add_argument("--dump", default=None, help="write one JSONL line per match (trace)")
    ap.add_argument("--allow-errors", action="store_true",
                    help="keep going after a match raises (default: raise)")
    args = ap.parse_args(argv)

    matches = args.matches
    if args.quick and args.matches == 8 and matches > 4:
        matches = 4
    if matches < 1:
        ap.error("--matches must be >= 1")

    duration = args.duration
    if duration is None and args.quick:
        duration = 150.0
    if duration is not None and duration <= 0:
        ap.error("--duration must be positive")

    base_cfg = kitcfg.base_config()
    overrides = []
    for raw in args.knob:
        if "=" not in raw:
            ap.error("--knob expects path=value, got %r" % raw)
        path, val = raw.split("=", 1)
        path = path.strip()
        if not path:
            ap.error("--knob path is empty: %r" % raw)
        try:
            value = _parse_literal(val)
        except Exception:
            ap.error("cannot parse --knob value %r" % val)
        overrides.append((path, value))
        kitcfg.set_path(base_cfg, path, value)

    factory = _make_bot_factory(args.bot)
    t_start = time.time()
    results = []
    dumps = []

    if not args.quiet:
        print("Mech Royale simulation - bot=%s matches=%d seed=%d map=%s"
              % (args.bot, matches, args.seed, kitcfg.MAP_DIR))
        if overrides:
            print("knobs: " + ", ".join("%s=%r" % kv for kv in overrides))
        if args.variant:
            print("variant: %s" % args.variant)
        if args.quick or duration is not None:
            print("duration cap: %.0f s" % (duration if duration is not None else 480.0))
        print("house bots: easy %d / medium %d / hard %d / expert %d"
              % LEVEL_COUNTS)
        print("house sensing: %s" % args.house_vision)

    for i in range(matches):
        seed = args.seed + i * 10007
        hello = kitcfg.make_hello(seed, config=copy.deepcopy(base_cfg), map_dir=kitcfg.MAP_DIR)
        try:
            bot = factory(hello)
        except Exception:
            print("FAILED constructing bot for match %d:" % (i + 1), file=sys.stderr)
            traceback.print_exc()
            if args.allow_errors:
                continue
            raise
        _patch_variant(bot, args.variant)
        match = Match(
            bot, hello, seed,
            duration=duration,
            no_fire=(args.variant == "no-fire"),
            trace_every=40 if args.quick else 20,
            house_vision=args.house_vision,
        )
        try:
            result = match.run()
        except Exception:
            print("FAILED in match %d (seed %d):" % (i + 1, seed), file=sys.stderr)
            traceback.print_exc()
            if args.allow_errors:
                continue
            raise
        result["match"] = i + 1
        result["bot"] = args.bot
        result["knobs"] = {p: v for p, v in overrides}
        result["variant"] = args.variant
        results.append(result)
        if not args.quiet:
            print(_fmt_match(i, result))
        if args.dump:
            dumps.append({
                "match": i + 1,
                "seed": seed,
                "variant": args.variant,
                "trace": result["trace"],
                "kill_log": result["kill_log"],
                "result": {k: v for k, v in result.items() if k not in ("trace", "kill_log")},
            })

    wall = time.time() - t_start
    if not results:
        print("no matches completed", file=sys.stderr)
        return 2

    summary = _summarize(results, wall)
    if not args.quiet:
        print("")
        _print_summary(summary, results)
    else:
        print(json.dumps(summary, sort_keys=True))

    payload = {
        "argv": sys.argv[1:] if argv is None else list(argv),
        "bot": args.bot,
        "matches": len(results),
        "seed": args.seed,
        "variant": args.variant,
        "house_vision": args.house_vision,
        "knobs": {p: v for p, v in overrides},
        "duration_cap": duration,
        "summary": summary,
        "results": results,
        "command": " ".join(sys.argv),
        "fidelity": "see docs/sim.md",
    }
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=1, sort_keys=True)
        if not args.quiet:
            print("json written to %s" % args.json_out)
    if args.dump:
        with open(args.dump, "w", encoding="utf-8") as fh:
            for row in dumps:
                fh.write(json.dumps(row, separators=(",", ":")) + "\n")
        if not args.quiet:
            print("replay dump written to %s" % args.dump)
    return 0


def _summarize(results, wall):
    n = len(results)
    scores = [r["score"] for r in results]
    places = [r["place"] for r in results]
    surv = [r["t"] for r in results]
    return {
        "matches": n,
        "mean_score": round(sum(scores) / n, 3),
        "mean_place": round(sum(places) / n, 3),
        "best_place": min(places),
        "worst_place": max(places),
        "wins": sum(1 for p in places if p == 1),
        "top4": sum(1 for p in places if p <= 4),
        "top8": sum(1 for p in places if p <= 8),
        "deaths": sum(1 for r in results if r["end"] == "death"),
        "timeouts": sum(1 for r in results if r["end"] == "time"),
        "mean_survival_s": round(sum(surv) / n, 2),
        "median_survival_s": round(sorted(surv)[n // 2], 2),
        "mean_kills": round(sum(r["kills"] for r in results) / n, 2),
        "mean_damage_dealt": round(sum(r["damage_dealt"] for r in results) / n, 1),
        "mean_damage_taken": round(sum(r["damage_taken"] for r in results) / n, 1),
        "mean_zone_damage": round(sum(r["zone_damage"] for r in results) / n, 1),
        "mean_time_outside_zone": round(sum(r["time_outside_zone"] for r in results) / n, 2),
        "mean_cubes": round(sum(r["cubes"] for r in results) / n, 2),
        "mean_act_ms": round(sum(r["act_ms_mean"] for r in results) / n, 3),
        "max_act_ms": max(r["act_ms_max"] for r in results),
        "over_budget": sum(r["over_budget"] for r in results),
        "bot_errors": sum(r["bot_errors"] for r in results),
        "bot_fails": sum(r.get("bot_fails", 0) for r in results),
        "house_vision": results[0].get("house_vision"),
        "mean_house_sight_avg": round(sum(r["house_sight_avg"] for r in results) / n, 3),
        "max_house_sight": max(r["house_sight_max"] for r in results),
        "mean_house_pings": round(sum(r["house_pings"] for r in results) / n, 2),
        "wall_s": round(wall, 2),
        "sec_per_match": round(wall / n, 2),
        "places": {str(p): places.count(p) for p in sorted(set(places))},
    }


def _print_summary(s, results):
    print("summary")
    print("  mean score      : %.3f   (16 - place, higher is better)" % s["mean_score"])
    print("  mean place      : %.2f   best %d / worst %d" % (s["mean_place"], s["best_place"], s["worst_place"]))
    print("  wins/top4/top8  : %d / %d / %d of %d" % (s["wins"], s["top4"], s["top8"], s["matches"]))
    print("  deaths/timeouts : %d / %d" % (s["deaths"], s["timeouts"]))
    print("  survival        : mean %.1f s, median %.1f s" % (s["mean_survival_s"], s["median_survival_s"]))
    print("  kills/damage    : %.2f kills, %.1f dealt, %.1f taken, %.1f zone"
          % (s["mean_kills"], s["mean_damage_dealt"], s["mean_damage_taken"], s["mean_zone_damage"]))
    print("  zone exposure   : %.2f s outside, %.1f zone damage" % (s["mean_time_outside_zone"], s["mean_zone_damage"]))
    print("  cubes picked    : %.2f" % s["mean_cubes"])
    print("  house sensing   : %s, sight avg %.2f houses (max %d), pings %.1f/match"
          % (s["house_vision"], s["mean_house_sight_avg"], s["max_house_sight"],
             s["mean_house_pings"]))
    print("  bot decision    : mean %.2f ms, max %.2f ms, over 50 ms: %d, errors: %d, "
          "policy fallbacks: %d"
          % (s["mean_act_ms"], s["max_act_ms"], s["over_budget"], s["bot_errors"],
             s["bot_fails"]))
    print("  wall            : %.1f s total, %.2f s/match" % (s["wall_s"], s["sec_per_match"]))
    print("  placements      : " + ", ".join("%s:%d" % (k, v) for k, v in s["places"].items()))


if __name__ == "__main__":
    sys.exit(main())
