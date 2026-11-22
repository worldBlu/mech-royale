# Navigation data and pathfinding

The navigator runs on a 2 m raster of the match arena. Rasterising it from `world.json`
(terrain heightfield + prisms + trees) costs ~0.8 s, which happens while the match clock is
already running, so the grids are baked offline into `bot/data/nav.bin` and loaded at startup
instead. The file is pure data: no pickle, no code execution.

Everything here is standard library only and works on the `python:3.13-slim` match image.

Besides `plan`/`steer`, the navigator exposes `World.cover_quality(x, y)` and
`Navigator.steer_cover(pos, goal, threat_pos, now)` for the policy's crossfire avoidance; see
[Cover steering](#cover-steering-steer_cover) below.

## Grid model

* Cell size `CELL = 2.0` m. `nx = ceil((max_x - min_x) / CELL) + 1 = 535`,
  `ny = ceil((max_y - min_y) / CELL) + 1 = 362` (193,670 cells) for the shipped map.
* Cell `(i, j)` has its centre at `(min_x + i * CELL, min_y + j * CELL)`, row-major with `i`
  fastest: index `k = j * nx + i`.
* Per cell arrays, same length `n = nx * ny`:
  * `walk` — 1 means blocked for walking (`World.walkable`), 0 means walkable;
  * `sight` — 1 means line of sight is blocked (`World.has_los`), 0 clear;
  * `height` — ground height in int16 centimetres (quantisation error ≤ 0.005 m);
  * `comp` — 4-connected walkable-region label, 255 for blocked cells. The main region is 0
    (140,122 cells); there are 57 regions in total.

`walk` and `sight` use the same rules as the fallback rasteriser, so both code paths answer
identically:

* terrain slope above 50° blocks walking;
* a prism blocks walking when its base is at or below `height + STEP_UP (1.2 m)` and its top is
  above that; a bridge (base above the current ground) is walkable underneath, a building is
  not;
* prisms block sight when they intersect the eye band (`base < height + 8 m` and
  `top > height + 1.5 m`) — trees block sight only through their trunks.

`World` also keeps a padded copy (`walkp`, stride `nx + 2`, one blocked-cell border) and the
raw terrain heightfield for `terrain_z`, so replay/sim tooling keeps working.

## `bot/data/nav.bin` format

72-byte header, little endian, followed by one zlib payload:

| offset | type        | field                                              |
|-------:|-------------|----------------------------------------------------|
|      0 | `8s`        | magic `b"MECHNAV1"`                                |
|      8 | `u32`       | format version (`1`)                               |
|     12 | `u32`       | header size (`72`)                                 |
|     16 | `u32`       | map fingerprint (see below)                        |
|     20 | `f32`       | `CELL`                                             |
|     24 | `f32`       | `min_x`                                            |
|     28 | `f32`       | `min_y`                                            |
|     32 | `f32`       | `max_x`                                            |
|     36 | `f32`       | `max_y`                                            |
|     40 | `u32`       | `nx`                                               |
|     44 | `u32`       | `ny`                                               |
|     48 | `u32`       | payload length in bytes (compressed)               |
|     52 | `u32`       | `walk` length (`n`)                                |
|     56 | `u32`       | `sight` length (`n`)                               |
|     60 | `u32`       | `height` length (`2 * n`, int16 LE centimetres)    |
|     64 | `u32`       | `comp` length (`n`)                                |
|     68 | `u32`       | CRC32 of the compressed payload                    |

Payload: `zlib.decompress` gives `walk (n) | sight (n) | height (2n) | comp (n)`, in that
order. Heights are signed int16 little-endian centimetres; everything else is one byte per
cell.

Fingerprint: `crc32(world.json bytes)` chained with `crc32(terrain.i16 bytes, previous)`. It
changes whenever the map changes, so a stale bake is rejected rather than used.

Loading (`World._load_bake`) validates magic, version, header size, cell size, grid size,
fingerprint, `abs(header bounds - json bounds) <= 1e-3` (bounds go through f32), the four array
lengths, file length against the compressed payload length, the payload CRC32, the decompressed
length and the int16 itemsize. Any mismatch — missing file, corrupted file, different map —
falls back to rasterising `world.json` in pure Python. The fallback was measured at 852 ms
ready with `kit/check`; the baked path is ~7.9 ms for `World` and 61 ms end-to-end ready.

## Baking

```powershell
# from the workspace root, with any Python 3.12/3.13
python tools/bake_map.py
```

Options: `--map <dir>` (default `kit/arena`), `--out <file>` (default `bot/data/nav.bin`),
`--skip-verify`. The tool rasterises the map, builds the region labels, packs the payload,
bakes a **second** copy and refuses to write unless the two byte strings are identical, then
writes atomically (`tmp` + `os.replace`). The shipped file:

* 244,166 bytes (72-byte header + 244,094-byte zlib level 9 payload; raw payload 968,350 bytes)
* fingerprint `0x61d1805c`, sha256 `0d8ba30a8fd9580024251a84403f6c39fd409a8d686462b4989f7e91f59b70ce`
* bake timings on the dev machine: rasterise 755 ms, region labels 37 ms, pack 102 ms

`MECH_NAV_BAKE=<path>` overrides the file at runtime (used by the bench to prove that a
corrupted bake is rejected); the match itself always reads `bot/data/nav.bin`.

## Pathfinding (`bot/nav.py`)

`Navigator(world, cfg=None, budget=1800)` keeps A* state in persistent arrays on the padded
grid (`g` float32, `stamp`/`closed`/`parent` int32, generation-stamped to avoid clearing
between searches), so no per-search allocation or dict hashing happens in the inner loop.

Per `plan(pos, goal)`:

1. clamp the goal into the map and snap it to `nearest_walkable`; plans shorter than 2 m
   return `[goal]` directly; if the *start* is inside a blocked cell (a shove can put the
   mech in scenery) it is snapped the same way and smoothing starts from the walkable point,
   and a start cell with no walkable neighbour returns `[]`;
2. if `World.path_clear(pos, goal)` holds (42% of an earlier 205-request capture, 66% in the
   latest 300-tick replay) return `[goal]` immediately;
3. otherwise reuse the tail of the previous path when the goal moved at most 4 m and a
   waypoint still has clear line of sight from `pos` (this is what keeps the per-tick cost
   down during a chase);
4. otherwise run weighted A* (`h * 1.05` on push, octile heuristic) with 8-neighbour moves and
   no corner cutting — a diagonal move requires both orthogonal cells to be walkable;
5. if the goal's region label differs from the start's, return `[]` (the policy's own stuck
   handling takes over) instead of exhausting the budget proving unreachability;
6. smooth the raw cell path: from each anchor, step up to 24 cells ahead and take the first
   waypoint whose straight leg passes `World.path_clear`. The real goal is appended only when
   that leg is clear — a partial path never fakes the last segment (the old code appended the
   goal unconditionally, which could make the bot walk into a wall).

When the 1800-expansion budget runs out, the lowest-heuristic closed node becomes the end of a
**partial** path; the next plan continues from wherever the bot actually is. `plan` always
returns waypoints that are walkable and mutually visible.

### Clearance guarantees

`World.path_clear` (used by `plan`, `_smooth`, `steer` and the bench) does an **exact cell
traversal** (DDA) of the segment, not point sampling: it visits every cell the straight line
enters, rejects any blocked or off-map cell, and treats an exact corner crossing as clear only
when both side cells are walkable too. Consequences:

* any sub-segment of a clear segment is also reported clear, so steering between replans cannot
  cut a corner that the planned leg passed;
* a segment that merely grazes a blocked cell at a corner is rejected, so a later 2 m step
  cannot slip into that cell;
* previously the checker sampled points at `dist/steps` spacing and accepted segments shorter
  than 0.5 m unconditionally, which let the mech creep into blocked cells in half-metre steps.
  The DDA costs roughly 2x the old sampler and still leaves `plan` p99 under 4 ms.

`steer` only drops a waypoint the mech already stands on when the leg to the *next* waypoint is
also clear; if dropping it leaves the bot off the verified line it replans from the real
position before steering. When `plan` returns nothing it does **not** aim blindly at the goal:
`_fallback_aim` checks the whole line, and otherwise commits only a probed point at most 2 m
away (halving down to 0.4 m) that is verified clear, so each tick re-probes from the position
actually reached. The bot can therefore never walk a guessed straight line into a wall.

Measured on this machine (not the 1-CPU match container), 200 random routes ≥ 250 m apart,
budget 1800:

| measurement              | baked nav.bin | rasterised fallback |
|--------------------------|---------------|---------------------|
| `World` ready            | 7.9 ms        | 821 ms              |
| `kit/check` ready        | 61 ms         | 852 ms              |
| plan p50 / p90 / p99 ms  | 2.85 / 3.42 / 3.65 | 2.89 / 3.36 / 3.68 |
| plan max ms              | 3.84          | 3.80                |
| expansions per plan      | 1800 (capped, p50 = p99) | same |
| single-shot goal reached | 33% (partial paths) | 28% |
| closed-loop reach        | 98% (195/200) | 86% (172/200)       |
| waypoints crossing walls | 0             | 0                   |

(Run-to-run variance is about ±0.3 ms on this machine.)

These 200 random long routes are the worst case: most plans exhaust the budget and are
partial. A replay of the real 300-tick `kit/check/stream.jsonl.gz` stream through the actual
policy (`Bot.act`) is much lighter — 164 plans, 109 of them one-leg line-of-sight returns:

| real-request replay | n | p50 | p90 | p99 | max |
|---------------------|---|-----|-----|-----|-----|
| `Navigator.plan`    | 164 | 0.01 | 3.04 | 3.42 | 3.47 |
| `Navigator.steer`   | 300 | 0.01 | 2.76 | 3.40 | 3.59 |
| whole tick          | 300 | 0.11 | 2.89 | 3.49 | 3.73 |

That replay validates all 443 output path legs with `World.path_clear`: 0 wall crossings.
Plan sources: astar 48, line-of-sight 109, suffix reuse 4, other-region 2, no goal / sealed
start 1.

Single-shot reach is low on purpose: one plan under the budget is a partial path on long
routes, and the closed-loop row is the honest "can the bot get there by replanning" number.
The fallback is worse only because without `comp` labels it cannot prune goals in another
region, so those pairs burn the budget and stall; the shipped path uses the bake.

Known limitation: the 5 closed-loop stalls above are corridor pockets where a start cell has
a single walkable neighbour and the only exit runs *away* from the goal, so the octile
heuristic cannot improve until the search wraps around a large building (1,800 expansions
close nodes but no closed node beats the start's heuristic; the proof search needs
2,000–4,000 expansions). The policy's own stuck/jump/detour handling escapes such a cell,
and the budget stays at 1,800 because raising it would put p99 over 5 ms. There is also a
one-cell forward step in the smoother for corridors that turn at every cell, so `plan` never
returns a path that is only the start point.

`kit/check/check.py` with the bake in place: `ready in 61 ms: name 'mech-royale'`,
`answer time ms: p50 0.2 p90 3.0 p99 3.7 max 3.9`, 300/300 answered, PASS. Deleting
`bot/data/nav.bin` still PASSes (`ready in 852 ms`, `p99 3.7 max 4.1`), so a missing data file
is safe.

## Cover steering (`steer_cover`)

`World.cover_quality(x, y)` returns 0..1: the fraction of the eight surrounding 2 m cells whose
straight line to `(x, y)` is blocked by the `sight` grid. Out-of-map neighbours count as
blocked, a blocked neighbour cell counts as blocked, and otherwise the line from the neighbour
centre to the point is walked exactly through the sight grid (endpoint excluded). It allocates
nothing and costs about 4 µs per call. Over 20,000 random walkable points: mean 0.038,
p50 0.00, p90 0.12, p99 0.50, max 1.00 — most open ground has no local cover, so the score is
deliberately local. It agrees with `World.has_los` neighbour counts (2/392 sampled points differ
by more than one neighbour, i.e. by a boundary cell).

`Navigator.steer_cover(pos, goal, threat_pos, now, force=False)` calls `steer` and then, only
on a tick where `steer` actually replanned, tries to move the **first** waypoint a few metres
sideways/forward. Candidates come from seven offsets (2 m and 3 m lateral, 2 m lateral ±1.6 m
forward, 2 m straight back). A candidate is accepted only when:

* it is inside the map and walkable;
* both legs `pos -> candidate` and `candidate -> next waypoint (or goal)` are clear
  (`World.path_clear`, exact DDA);
* the path length grows by at most `COVER_MAX_DETOUR = 15 %` (+0.25 m slack);
* `cover_quality` improves by at least `COVER_MIN_GAIN = 0.0625` (half a neighbour cell).

Among equally covered candidates the one the threat cannot see (`has_los`) wins, then the
shortest detour. The shift is applied once per planned waypoint (`_cover_target`), never again
to the same point, so the mech cannot creep sideways tick after tick; the returned point is
always a verified walkable one, so `steer_cover` can never return a blocked cell. A `None`
`threat_pos` degrades to plain `steer`. The signature mirrors `steer` (returns `(dx, dy,
distance_left)`) and sets `last_cover` / `last_cover_shift` for logging. Cost is bounded: on
replan ticks seven candidates × (two `path_clear` + one `cover_quality`), roughly 50 µs per
shift tick; on non-replan ticks it is exactly `steer`.

The bench `--cover` mode walks the same 200 routes twice at 20 Hz in 2 m steps, placing an enemy
30–90 m off each route midpoint (snapped to walkable ground), once with `steer` and once with
`steer_cover`. Measured here, baked grids:

| cover measurement             | `steer` off        | `steer_cover` on   |
|-------------------------------|--------------------|--------------------|
| steer call µs p50/p90/p99/max | 0.6 / 7.4 / 25.9 / 4616 | 0.8 / 55.3 / 200.2 / 4735 |
| plan p50 / p90 / p99 ms       | 0.01 / 0.02 / 3.75 | 0.01 / 0.02 / 3.74 |
| plan max ms                   | 4.61               | 4.65               |
| routes reached                | 105/200            | 103/200            |
| steps crossing a blocked cell | 0                  | 0                  |

Cover was used on 144/200 routes. Extra path length over routes both modes reached (n = 100):
p50 1.000, p90 1.004, max 1.006 — a 0.6 % worst case, far under the 15 % cap. The steer
p99/max figures include the internal `plan` on replan ticks; plan time is measured separately by
wrapping `Navigator.plan`. The 2-route reach difference is the fixed 2 m step cap on routes that
end just beyond it, not a wall.

Wiring: the policy should call `nav.steer_cover(pos, goal, threat_pos, t)` where it currently
calls `nav.steer(pos, goal, t)`, with the nearest dangerous enemy as `threat_pos` (or `None`).
Nothing else changes.

## Bench (`tools/bench_nav.py`)

```powershell
python tools/bench_nav.py              # baked if present
python tools/bench_nav.py --fallback   # force the rasteriser
python tools/bench_nav.py --n 400 --seed 11 --min-dist 200
python tools/bench_nav.py --cover      # cover steering on vs off, same routes
python tools/bench_nav.py --units      # unit checks only
```

It samples walkable cell centres in the main region (`--n 200 --seed 7 --min-dist 250` by
default), plans once per pair, then closes the loop: replan from the end of the last partial
path up to `--max-iter` times and count honest reach. Every returned path is validated leg by
leg with `World.path_clear`; a single blocked or off-map leg fails the run. It also proves the
bake gate (a corrupted copy of `nav.bin` must be rejected and the fallback grids must equal the
baked ones) and the suffix-reuse path. PASS requires zero violations, plan p99 ≤ 5 ms,
closed-loop reach ≥ 85% of reachable pairs, working reuse and a working bake gate.

`--cover` adds the cover comparison shown in the previous section; it fails the run when a
steering step crosses a blocked cell, cover plan p99 > 5 ms, a cover shift costs more than 15 %
length at p90, or cover steering loses more than two routes of reach.

`--units` runs the deterministic checks without the 200-route walk:

```
units     blocked start (187,1): 7 waypoints, 0 bad legs -> OK
units     sealed start (109,162): 0 waypoints, source 'unreachable' -> OK
units     narrow corridor: 5/5 zigzag points kept, legs visible: yes -> OK
units     path_clear DDA: 108 clear segments (0 sub-segment mismatches), 200/200 blocked-endpoint lines rejected -> OK
```

* **blocked start** — the mech is pushed into a blocked cell next to main-region ground; `plan`
  must still return a path whose waypoints are all walkable and whose legs are clear (it snaps
  the start with `nearest_walkable` and smooths from there).
* **sealed start** — a walkable cell with no walkable 4-neighbour (a one-cell region); `plan`
  must return `[]` and report `stuck`/`unreachable` instead of faking a path.
* **narrow corridor** — a duck-typed stub world whose `path_clear` only allows consecutive
  points of a zigzag corridor, with the mech starting slightly off the line; smoothing must keep
  every point and every leg visible rather than giving up or repeating the start point.
* **path_clear DDA** — random clear segments are split and every sub-segment must also be clear
  (108 segments, 0 mismatches), and lines ending in a blocked cell must be rejected (200/200).

Last runs: baked `PASS` — plan p50/p90/p99 2.85/3.42/3.65 ms, 0 violations, closed-loop reach
98% (195/200, 5 corridor-pocket stalls), reuse 17/17, bake gate OK; `--fallback` `PASS` — plan
p99 3.68 ms, closed-loop 86% (172/200), 0 violations; `--cover` `PASS` — plan p99 3.74 ms,
extra length p90 0.4 %, 0 blocked steps; `--units` `PASS`.
