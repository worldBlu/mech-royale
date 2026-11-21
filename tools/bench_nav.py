#!/usr/bin/env python3
"""Benchmark the navigator on long random routes.

    python tools/bench_nav.py                      # shipped config (baked grids if present)
    python tools/bench_nav.py --fallback           # force rasterising from world.json
    python tools/bench_nav.py --n 400 --seed 11

Two measurements over N random pairs of walkable cells at least --min-dist metres apart:

  * single shot: one plan() from the start cell centre, timed with perf_counter
  * closed loop: plan, stand at the end of the returned (possibly partial) path, replan, up to
    --max-iter times -- this is how the policy actually travels, so it measures honest reach

Every returned path is checked segment by segment (start -> waypoint 1 -> ... -> last) with
World.path_clear; a path that crosses a blocked cell or leaves the map is a violation and fails
the run. The run also proves the bake gate: a corrupted copy of nav.bin must be rejected and the
bot must still build the same grids by rasterising.

Exit code 0 = PASS, 1 = FAIL.
"""
import argparse
import math
import os
import random
import shutil
import struct
import sys
import tempfile
import time
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BOT = os.path.join(ROOT, "bot")
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import world as navworld                        # noqa: E402
from nav import Navigator                       # noqa: E402

DEFAULT_MAP = os.path.join(ROOT, "kit", "arena")

# PASS thresholds. The p99 budget is the task's: one plan must stay under 5 ms on one core.
MAX_P99_MS = 5.0
MIN_REACH = 0.85


def pct(values, q):
    if not values:
        return 0.0
    k = int(round((len(values) - 1) * q))
    return values[k]


def stats_line(name, values):
    values = sorted(values)
    return ("%-10s n %d  p50 %.2f  p90 %.2f  p99 %.2f  max %.2f"
            % (name, len(values), pct(values, 0.5), pct(values, 0.9),
               pct(values, 0.99), values[-1] if values else 0.0))


def check_path(world, pos, path):
    """Return the number of waypoints whose straight leg from the previous point is blocked."""
    bad = 0
    px, py = pos[0], pos[1]
    for p in path:
        if not world.path_clear(px, py, p[0], p[1]):
            bad += 1
        px, py = p[0], p[1]
    return bad


def pick_pairs(world, n, min_dist, rng):
    """N pairs of walkable cell centres, at least min_dist apart (main region when known)."""
    cells = []
    comp = getattr(world, "comp", None)
    for j in range(world.ny):
        row = j * world.nx
        for i in range(world.nx):
            if world.walk[row + i]:
                continue
            if comp is not None and comp[row + i] != 0:
                continue                    # keep to the main walkable region
            cells.append((i, j))
    if len(cells) < 2:
        raise SystemExit("bench: not enough walkable cells")
    out = []
    tries = 0
    while len(out) < n and tries < n * 200:
        tries += 1
        a = cells[rng.randrange(len(cells))]
        b = cells[rng.randrange(len(cells))]
        pa = world.cell_xy(*a)
        pb = world.cell_xy(*b)
        if math.dist(pa, pb) < min_dist:
            continue
        out.append((pa, pb))
    if len(out) < n:
        raise SystemExit("bench: only %d of %d pairs found at %.0f m" % (len(out), n, min_dist))
    return out


def reuse_selftest(world, nav, pos, goal):
    """plan() must reuse a valid suffix of the previous path when the goal barely moved."""
    nav.path = nav.plan(pos, goal)
    nav.goal = goal
    if len(nav.path) < 2:
        return None
    pos2 = nav.path[1]
    goal2 = (goal[0] + 1.0, goal[1] + 0.5)
    if math.dist(goal, goal2) >= 4.0:
        return None
    path = nav.plan(pos2, goal2)
    ok = nav.last_source == "reuse" and check_path(world, pos2, path) == 0
    return ok


def bake_gate_selftest(map_dir, tmpdir):
    """A corrupted bake file must be rejected, and the fallback must rebuild the same grids."""
    ref = navworld.World(map_dir)
    if not ref.baked:
        return "skipped (no bake loaded)"
    bad = bytearray(open(navworld.BAKE_FILE, "rb").read())
    bad[-100] ^= 0xFF
    path = os.path.join(tmpdir, "corrupt_nav.bin")
    with open(path, "wb") as f:
        f.write(bad)
    old = os.environ.get(navworld.BAKE_ENV)
    os.environ[navworld.BAKE_ENV] = path
    try:
        w = navworld.World(map_dir)
    finally:
        if old is None:
            os.environ.pop(navworld.BAKE_ENV, None)
        else:
            os.environ[navworld.BAKE_ENV] = old
    ok = (not w.baked) and w.walk == ref.walk and w.sight == ref.sight
    return "OK (fell back to world.json)" if ok else "FAIL"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--map", default=DEFAULT_MAP)
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--min-dist", type=float, default=250.0)
    ap.add_argument("--max-iter", type=int, default=40)
    ap.add_argument("--fallback", action="store_true", help="ignore nav.bin and rasterise")
    args = ap.parse_args(argv)

    t0 = time.perf_counter()
    world = navworld.World(os.path.abspath(args.map), use_bake=not args.fallback)
    ready_ms = (time.perf_counter() - t0) * 1000.0
    nav = Navigator(world)
    print("world     %s  (%d x %d cells, %d walkable, %d regions)"
          % ("baked nav.bin" if world.baked else "rasterised fallback",
             world.nx, world.ny, sum(1 for b in world.walk if not b),
             (max(c for c in world.comp if c != 255) + 1) if world.comp is not None else 0))
    print("ready     World %.1f ms (%s), budget %d expansions, replan %.2f s / %.1f m"
          % (ready_ms, "baked" if world.baked else "fallback", nav.budget,
             nav.replan_s, nav.goal_shift))

    rng = random.Random(args.seed)
    pairs = pick_pairs(world, args.n, args.min_dist, rng)
    print("pairs     %d random routes, min straight-line distance %.0f m"
          % (len(pairs), args.min_dist))

    # ------------------------------------------------------------------ single shot
    plan_ms = []
    expansions = []
    sources = {}
    violations = 0
    reached = 0
    empty = 0
    for pos, goal in pairs:
        t = time.perf_counter()
        path = nav.plan(pos, goal)
        plan_ms.append((time.perf_counter() - t) * 1000.0)
        expansions.append(nav.last_expansions)
        sources[nav.last_source] = sources.get(nav.last_source, 0) + 1
        violations += check_path(world, pos, path)
        if not path:
            empty += 1
        elif math.dist(path[-1], goal) <= 6.0:
            reached += 1
    print()
    print(stats_line("plan ms", plan_ms))
    print(stats_line("expansions", [float(e) for e in expansions]))
    print("single    reached goal %.0f%% (%d/%d), empty paths %d, violations %d"
          % (100.0 * reached / len(pairs), reached, len(pairs), empty, violations))
    print("sources   " + "  ".join("%s %d" % kv for kv in sorted(sources.items())))

    # ------------------------------------------------------------------ closed loop
    comp = getattr(world, "comp", None)
    loop_ms = []
    loop_exp = []
    steps = []
    reachable = 0
    reached_loop = 0
    stalled = 0
    unreachable = 0
    loop_violations = 0
    t_loop = time.perf_counter()
    for pos, goal in pairs:
        if comp is not None:
            ci, cj = world.cell_of(*pos)
            gi, gj = world.cell_of(*goal)
            if comp[cj * world.nx + ci] != comp[gj * world.nx + gi]:
                unreachable += 1
                continue
        reachable += 1
        p = pos
        ok = False
        n = 0
        while n < args.max_iter:
            t = time.perf_counter()
            path = nav.plan(p, goal)
            loop_ms.append((time.perf_counter() - t) * 1000.0)
            loop_exp.append(nav.last_expansions)
            n += 1
            loop_violations += check_path(world, p, path)
            if not path:
                stalled += 1
                break
            nxt = path[-1]
            if math.dist(nxt, p) < 0.5:     # no forward progress at all
                stalled += 1
                break
            p = nxt
            if math.dist(p, goal) <= 6.0:
                ok = True
                break
        steps.append(float(n))
        if ok:
            reached_loop += 1
    loop_s = time.perf_counter() - t_loop
    print()
    print(stats_line("loop plan", loop_ms))
    print(stats_line("loop exp", [float(e) for e in loop_exp]))
    print(stats_line("replans", steps))
    rate = reached_loop / reachable if reachable else 0.0
    print("closed    reached goal %.0f%% (%d/%d reachable pairs), %d pairs in other regions, "
          "stalled %d, violations %d, %.1f s"
          % (100.0 * rate, reached_loop, reachable, unreachable, stalled,
             loop_violations, loop_s))

    # ------------------------------------------------------------------ self tests
    print()
    gate = bake_gate_selftest(os.path.abspath(args.map), tempfile.mkdtemp(prefix="navbench_"))
    print("bake gate %s" % gate)

    reuse_fails = 0
    reuse_runs = 0
    for pos, goal in pairs[:20]:
        r = reuse_selftest(world, nav, pos, goal)
        if r is None:
            continue
        reuse_runs += 1
        if not r:
            reuse_fails += 1
    print("reuse     %d/%d probes reused a verified suffix"
          % (reuse_runs - reuse_fails, reuse_runs))

    # ------------------------------------------------------------------ verdict
    p99 = pct(sorted(plan_ms), 0.99)
    problems = []
    if violations or loop_violations:
        problems.append("%d segments cross blocked cells" % (violations + loop_violations))
    if p99 > MAX_P99_MS:
        problems.append("plan p99 %.2f ms > %.1f ms" % (p99, MAX_P99_MS))
    if rate < MIN_REACH:
        problems.append("closed-loop reach %.0f%% < %.0f%%" % (100.0 * rate, 100.0 * MIN_REACH))
    if reuse_runs and reuse_fails:
        problems.append("%d reuse probes did not reuse" % reuse_fails)
    if not gate.startswith("OK"):
        problems.append("bake gate %s" % gate)
    print()
    print("FAIL: " + "; ".join(problems) if problems else "PASS")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
