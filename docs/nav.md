# Navigation data and pathfinding

The navigator runs on a 2 m raster of the match arena. Rasterising it from `world.json`
(terrain heightfield + prisms + trees) costs ~0.8 s, which happens while the match clock is
already running, so the grids are baked offline into `bot/data/nav.bin` and loaded at startup
instead. The file is pure data: no pickle, no code execution.

Everything here is standard library only and works on the `python:3.13-slim` match image.

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
falls back to rasterising `world.json` in pure Python. The fallback was measured at 867 ms
ready with `kit/check`; the baked path is ~7.8 ms for `World` and 57 ms end-to-end ready.

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
2. if `World.path_clear(pos, goal)` holds (42% of real requests) return `[goal]` immediately;
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
returns waypoints that are walkable and mutually visible; `steer` keeps the same signature and
fallback behaviour as before, but if plan returns nothing it aims straight at the goal (instead
of returning `None`, which would stop the mech).

Measured on this machine (not the 1-CPU match container), 200 random routes ≥ 250 m apart,
budget 1800:

| measurement              | baked nav.bin | rasterised fallback |
|--------------------------|---------------|---------------------|
| `World` ready            | 7.8 ms        | 780 ms              |
| `kit/check` ready        | 57 ms         | 867 ms              |
| plan p50 / p90 / p99 ms  | 2.72 / 3.10 / 3.32 | 2.74 / 3.01 / 3.26 |
| plan max ms              | 3.41          | 3.64                |
| expansions per plan      | 1800 (capped, p50 = p99) | same |
| single-shot goal reached | 33% (partial paths) | 28% |
| closed-loop reach        | 98% (195/200) | 86% (172/200)       |
| waypoints crossing walls | 0             | 0                   |

These 200 random long routes are the worst case: most plans exhaust the budget and are
partial. A replay of the real 300-tick `kit/check/stream.jsonl.gz` stream through the actual
policy (`Bot.act`) is much lighter — 212 plans, 79 of them one-leg line-of-sight returns:

| real-request replay | n | p50 | p90 | p99 | max |
|---------------------|---|-----|-----|-----|-----|
| `Navigator.plan`    | 212 | 0.80 | 2.93 | 3.16 | 3.18 |
| `Navigator.steer`   | 300 | 0.05 | 2.90 | 3.20 | 3.22 |
| whole tick          | 300 | 0.17 | 2.91 | 3.22 | 3.37 |

That replay validates all 930 output path legs with `World.path_clear`: 0 wall crossings.
Plan sources: astar 127, line-of-sight 79, other-region 4, no goal / sealed start 2.

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

`kit/check/check.py` with the bake in place: `ready in 57 ms: name 'mech-royale'`,
`answer time ms: p50 0.3 p90 3.1 p99 3.4 max 3.5`, 300/300 answered, PASS. Deleting
`bot/data/nav.bin` still PASSes (`ready in 867 ms`, `p99 3.4 max 3.8`), so a missing data file
is safe.

## Bench (`tools/bench_nav.py`)

```powershell
python tools/bench_nav.py              # baked if present
python tools/bench_nav.py --fallback   # force the rasteriser
python tools/bench_nav.py --n 400 --seed 11 --min-dist 200
```

It samples walkable cell centres in the main region (`--n 200 --seed 7 --min-dist 250` by
default), plans once per pair, then closes the loop: replan from the end of the last partial
path up to `--max-iter` times and count honest reach. Every returned path is validated leg by
leg with `World.path_clear`; a single blocked or off-map leg fails the run. It also proves the
bake gate (a corrupted copy of `nav.bin` must be rejected and the fallback grids must equal the
baked ones) and the suffix-reuse path. PASS requires zero violations, plan p99 ≤ 5 ms,
closed-loop reach ≥ 85% of reachable pairs, working reuse and a working bake gate.

Last run (baked): PASS — plan p99 3.32 ms, 0 violations, closed-loop reach 98% (195/200,
5 corridor-pocket stalls), reuse 17/17, bake gate OK.
