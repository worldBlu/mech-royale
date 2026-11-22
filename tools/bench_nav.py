#!/usr/bin/env python3
"""Benchmark the navigator on long random routes.

    python tools/bench_nav.py                      # shipped config (baked grids if present)
    python tools/bench_nav.py --fallback           # force rasterising from world.json
    python tools/bench_nav.py --n 400 --seed 11
    python tools/bench_nav.py --cover              # cover steering on vs off, same routes
    python tools/bench_nav.py --units              # unit checks (blocked/sealed start, corridor)

Two measurements over N random pairs of walkable cells at least --min-dist metres apart:

  * single shot: one plan() from the start cell centre, timed with perf_counter
  * closed loop: plan, stand at the end of the returned (possibly partial) path, replan, up to
    --max-iter times -- this is how the policy actually travels, so it measures honest reach

Every returned path is checked segment by segment (start -> waypoint 1 -> ... -> last) with
World.path_clear; a path that crosses a blocked cell or leaves the map is a violation and fails
the run. The run also proves the bake gate: a corrupted copy of nav.bin must be rejected and the
bot must still build the same grids by rasterising.

--cover walks every route twice -- Navigator.steer vs Navigator.steer_cover with the same
threat position -- at 20 Hz in 2 m steps and reports the extra path length (travel on/off), the
added steer and plan time, and any step that crosses a blocked cell. It asserts the cover-path
p99 plan time stays under 5 ms and that a shift never costs more than 15 % length at p90.

--units runs the small deterministic checks: a plan from a blocked cell (mech pushed into a wall)
must still return a valid walk, a sealed single-cell start must report unreachable, smoothing a
tight-corridor zigzag must return clear one-cell legs, and World.path_clear must agree with its
own sub-segments.

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

# Cover bench: a cover shift may add at most 15 % path length (mirrors nav.COVER_MAX_DETOUR).
COVER_STEP = 2.0
COVER_MAX_EXTRA = 0.15


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


def _timed_plan(orig, bucket):
    """Wrap nav.plan so each internal replan is recorded; the steering call is not."""
    def timed(pos, goal):
        t = time.perf_counter()
        out = orig(pos, goal)
        bucket.append((time.perf_counter() - t) * 1000.0)
        return out
    return timed


def pick_threat(world, a, b, rng):
    """A plausible enemy position: 30-90 m off the route midpoint, on walkable ground."""
    dx = b[0] - a[0]
    dy = b[1] - a[1]
    d = math.hypot(dx, dy) or 1.0
    side = -1.0 if rng.random() < 0.5 else 1.0
    off = rng.uniform(30.0, 90.0)
    mx = (a[0] + b[0]) * 0.5
    my = (a[1] + b[1]) * 0.5
    tx = mx - dy / d * side * off
    ty = my + dx / d * side * off
    n = world.nearest_walkable(tx, ty, max_r=120.0)
    return n if n is not None else (mx, my)


def walk_route(world, nav, start, goal, threat, cover, steer_us=None):
    """Walk a route in 2 m steps at 20 Hz. Returns (travel, steps, reached, bad, shifts)."""
    nav.path = []
    nav.goal = None
    nav.t_path = -1e9
    nav._cover_target = None
    pos = start
    now = 0.0
    travel = 0.0
    steps = 0
    bad = 0
    shifts = 0
    reached = False
    max_steps = min(700, int(math.dist(start, goal) / COVER_STEP) + 80)
    while steps < max_steps:
        t = time.perf_counter()
        out = nav.steer_cover(pos, goal, threat, now) if cover else nav.steer(pos, goal, now)
        if steer_us is not None:
            steer_us.append((time.perf_counter() - t) * 1e6)
        steps += 1
        if cover and nav.last_cover_shift > 0.0:
            shifts += 1
        if out is None:
            break
        dx, dy, dist = out
        if dist <= 3.0:
            reached = True
            break
        tgt = nav.path[0] if nav.path else goal
        s = min(COVER_STEP, math.dist(pos, tgt))
        if s < 0.2 or (dx == 0.0 and dy == 0.0):
            break
        nxt = (pos[0] + dx * s, pos[1] + dy * s)
        if not world.path_clear(pos[0], pos[1], nxt[0], nxt[1]):
            bad += 1
            break
        pos = nxt
        travel += s
        now += 0.05
    return travel, steps, reached, bad, shifts


def run_cover(world, nav, pairs, seed):
    """steer_cover vs steer over identical routes: extra length, extra time, violations."""
    rng = random.Random(seed ^ 0x5EED)
    orig_plan = nav.plan
    timed = {"off": [], "on": []}
    steer = {"off": [], "on": []}
    ratios = []
    reach = {"off": 0, "on": 0}
    routes = shifts = bad = skipped = 0
    try:
        for pos, goal in pairs:
            if not world.walkable(pos[0], pos[1]) or not world.walkable(goal[0], goal[1]):
                skipped += 1
                continue
            threat = pick_threat(world, pos, goal, rng)
            nav.plan = _timed_plan(orig_plan, timed["off"])
            off = walk_route(world, nav, pos, goal, threat, False, steer["off"])
            nav.plan = _timed_plan(orig_plan, timed["on"])
            on = walk_route(world, nav, pos, goal, threat, True, steer["on"])
            routes += 1
            bad += off[3] + on[3]
            if off[2]:
                reach["off"] += 1
            if on[2]:
                reach["on"] += 1
            if on[4]:
                shifts += 1
            if off[2] and on[2] and off[0] > 0.0:
                ratios.append(on[0] / off[0])
    finally:
        if "plan" in nav.__dict__:
            del nav.plan
    lines = ["cover     %d routes (%d skipped), cover shift used on %d routes"
             % (routes, skipped, shifts)]
    lines.append(stats_line("steer off", [float(v) for v in steer["off"]]))
    lines.append(stats_line("steer on", [float(v) for v in steer["on"]]))
    lines.append(stats_line("plan off", timed["off"]))
    lines.append(stats_line("plan on", timed["on"]))
    lines.append("cover len extra on/off (both reached, n %d): p50 %.3f  p90 %.3f  max %.3f"
                 % (len(ratios), pct(sorted(ratios), 0.5) if ratios else 0.0,
                    pct(sorted(ratios), 0.9) if ratios else 0.0,
                    max(ratios) if ratios else 0.0))
    lines.append("cover     reached off %d/%d, on %d/%d, steps crossing blocked cells %d"
                 % (reach["off"], routes, reach["on"], routes, bad))

    problems = []
    p99_plan = max(pct(sorted(timed["off"]), 0.99), pct(sorted(timed["on"]), 0.99))
    if p99_plan > MAX_P99_MS:
        problems.append("cover plan p99 %.2f ms > %.1f ms" % (p99_plan, MAX_P99_MS))
    if ratios and pct(sorted(ratios), 0.9) - 1.0 > COVER_MAX_EXTRA:
        problems.append("cover p90 extra length %.1f%% > %.0f%%"
                        % (100.0 * (pct(sorted(ratios), 0.9) - 1.0), 100.0 * COVER_MAX_EXTRA))
    if bad:
        problems.append("%d cover steering steps crossed a blocked cell" % bad)
    if reach["on"] < reach["off"] - 2:
        problems.append("cover reach %d < off reach %d" % (reach["on"], reach["off"]))
    return lines, problems


def _random_walkable_point(world, rng):
    for _ in range(4000):
        i = rng.randrange(world.nx)
        j = rng.randrange(world.ny)
        if not world.walk[j * world.nx + i]:
            return world.cell_xy(i, j)
    return world.cell_xy(world.nx // 2, world.ny // 2)


def _isolated_walkable(world):
    """A walkable cell with no walkable 4-neighbour (a sealed one-cell region)."""
    walk = world.walk
    nx = world.nx
    for j in range(1, world.ny - 1):
        row = j * nx
        for i in range(1, nx - 1):
            k = row + i
            if walk[k]:
                continue
            if walk[k - 1] and walk[k + 1] and walk[k - nx] and walk[k + nx]:
                return (i, j)
    return None


def _blocked_next_to_walkable(world):
    walk = world.walk
    nx = world.nx
    for j in range(1, world.ny - 1):
        row = j * nx
        for i in range(1, nx - 1):
            k = row + i
            if not walk[k]:
                continue
            if not (walk[k - 1] and walk[k + 1] and walk[k - nx] and walk[k + nx]):
                return (i, j)
    return None


def run_units(world, nav, rng):
    """Small deterministic checks that used to be ad-hoc scripts in the task notes."""
    lines = []
    problems = []
    nx = world.nx
    walk = world.walk
    comp = getattr(world, "comp", None)

    # 1. blocked start: the mech is pushed into a wall; plan must still return a valid walk.
    cell = _blocked_next_to_walkable(world)
    if cell is None:
        lines.append("units     blocked start: skipped (no blocked cell next to walkable ground)")
    else:
        i, j = cell
        pos = world.cell_xy(i, j)
        region = 0
        for di, dj in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            if not walk[(j + dj) * nx + (i + di)]:
                if comp is not None:
                    region = comp[(j + dj) * nx + (i + di)]
                break
        goal = None
        for _ in range(4000):
            gi = rng.randrange(nx)
            gj = rng.randrange(world.ny)
            if walk[gj * nx + gi]:
                continue
            if comp is not None and comp[gj * nx + gi] != region:
                continue
            p = world.cell_xy(gi, gj)
            if 60.0 <= math.dist(p, pos) <= 250.0:
                goal = p
                break
        if goal is None:
            goal = _random_walkable_point(world, rng)
        path = nav.plan(pos, goal)
        legs = check_path(world, path[0], path[1:]) if path else 1
        ok = bool(path) and all(world.walkable(p[0], p[1]) for p in path) and legs == 0
        lines.append("units     blocked start (%d,%d): %d waypoints, %d bad legs -> %s"
                     % (i, j, len(path), legs, "OK" if ok else "FAIL"))
        if not ok:
            problems.append("blocked-start plan at (%d,%d) is not a valid walk" % (i, j))

    # 2. sealed start: a one-cell region has nowhere to go; plan must say so, not fake a path.
    cell = _isolated_walkable(world)
    if cell is None:
        lines.append("units     sealed start: skipped (no sealed single-cell region found)")
    else:
        i, j = cell
        path = nav.plan(world.cell_xy(i, j), _random_walkable_point(world, rng))
        ok = path == [] and nav.last_source in ("stuck", "unreachable")
        lines.append("units     sealed start (%d,%d): %d waypoints, source '%s' -> %s"
                     % (i, j, len(path), nav.last_source, "OK" if ok else "FAIL"))
        if not ok:
            problems.append("sealed-start plan at (%d,%d) did not report unreachable" % (i, j))

    # 3. narrow-corridor smoothing: a zigzag corridor where only consecutive points are mutually
    #    visible (the stub only allows those pairs), and the mech starts off the line. No shortcut
    #    can be taken, so _smooth must fall back to advancing one cell at a time instead of giving
    #    up or repeating points; every returned leg must still be visible.
    pts = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0), (0.0, 2.0)]
    allowed = set()
    for k in range(len(pts) - 1):
        allowed.add((pts[k], pts[k + 1]))
        allowed.add((pts[k + 1], pts[k]))

    class _CorridorOnly:
        def path_clear(self, x0, y0, x1, y1):
            a = (x0, y0)
            b = (x1, y1)
            return a == b or (a, b) in allowed

    stub = Navigator.__new__(Navigator)
    stub.w = _CorridorOnly()
    out = stub._smooth(list(pts), (0.1, -0.1), pts[-1])
    legs = all(((out[k], out[k + 1]) in allowed or out[k] == out[k + 1])
               for k in range(len(out) - 1))
    ok = len(out) == len(pts) and legs and out[0] == pts[0] and out[-1] == pts[-1]
    lines.append("units     narrow corridor: %d/%d zigzag points kept, legs visible: %s -> %s"
                 % (len(out), len(pts), "yes" if legs else "no", "OK" if ok else "FAIL"))
    if not ok:
        problems.append("narrow-corridor smoothing lost a clear leg")

    # 4. path_clear must agree with its own sub-segments, and a line ending in a blocked cell
    #    must be rejected (the DDA checks the end cell, not just sampled points).
    rng2 = random.Random(4242)
    clear_segments = 0
    sub_fails = 0
    blocked_cells = [(i, j) for j in range(0, world.ny, 97)
                     for i in range(0, nx, 97) if walk[j * nx + i]]
    tested_blocks = 0
    rejected = 0
    for _ in range(800):
        a = _random_walkable_point(world, rng2)
        b = _random_walkable_point(world, rng2)
        if world.path_clear(a[0], a[1], b[0], b[1]):
            clear_segments += 1
            for f in (0.13, 0.37, 0.61, 0.89):
                q = (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f)
                if not (world.path_clear(a[0], a[1], q[0], q[1])
                        and world.path_clear(q[0], q[1], b[0], b[1])):
                    sub_fails += 1
                    break
    for _ in range(200):
        if not blocked_cells:
            break
        a = _random_walkable_point(world, rng2)
        bi, bj = blocked_cells[rng2.randrange(len(blocked_cells))]
        b = world.cell_xy(bi, bj)
        if world.walkable(b[0], b[1]):
            continue
        tested_blocks += 1
        if not world.path_clear(a[0], a[1], b[0], b[1]):
            rejected += 1
    ok = sub_fails == 0 and clear_segments > 0 and rejected == tested_blocks
    lines.append("units     path_clear DDA: %d clear segments (0 sub-segment mismatches), "
                 "%d/%d blocked-endpoint lines rejected -> %s"
                 % (clear_segments, rejected, tested_blocks, "OK" if ok else "FAIL"))
    if not ok:
        problems.append("path_clear disagrees with its own sub-segments")
    return lines, problems


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--map", default=DEFAULT_MAP)
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--min-dist", type=float, default=250.0)
    ap.add_argument("--max-iter", type=int, default=40)
    ap.add_argument("--fallback", action="store_true", help="ignore nav.bin and rasterise")
    ap.add_argument("--cover", action="store_true",
                    help="cover steering on vs off over the same routes (extra length/time)")
    ap.add_argument("--units", action="store_true",
                    help="unit checks only: blocked/sealed start, narrow corridor, DDA")
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

    if args.units:
        lines, problems = run_units(world, nav, random.Random(args.seed))
        for line in lines:
            print(line)
        print()
        print("FAIL: " + "; ".join(problems) if problems else "PASS")
        return 1 if problems else 0

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

    # ------------------------------------------------------------------ cover steering
    cover_problems = []
    if args.cover:
        print()
        cover_lines, cover_problems = run_cover(world, nav, pairs, args.seed)
        for line in cover_lines:
            print(line)

    # ------------------------------------------------------------------ verdict
    p99 = pct(sorted(plan_ms), 0.99)
    problems = list(cover_problems)
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
