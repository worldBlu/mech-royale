# Offline match simulator (`tools/sim/`)

A deterministic, standard-library-only approximation of a Mech Royale match, built to answer
one question: **does bot change A score better than bot change B, all else equal?**
It is not the official scorer. The official server has the real house-bot AI, the real movement
solver and the real 16-player matchmaking; this simulator approximates all three and documents
where it differs.

## Run it

From the repo root (a Python 3.12 interpreter is enough; nothing outside the stdlib is used):

```powershell
# 48 practice matches of bot/main.py (the scoring command; calibrated reference mode)
python tools/eval.py --matches 48 --house-vision vision

# Fast loop while tuning (4 matches, 150 s cap per match)
python tools/eval.py --quick --house-vision vision

# A/B two policy variants on the same seeds
python tools/eval.py --matches 24 --seed 7 --variant no-fire
python tools/eval.py --matches 24 --seed 7 --knob vision.range=120

# Score an exported older commit without touching bot/
python tools/eval.py --bot-dir .reports/bot_96478d5/bot --matches 48 --house-vision vision

# House-bot skill sensitivity (applies the override to every difficulty level)
python tools/eval.py --matches 24 --house-vision vision --house-knob aim_sigma_deg=3.0

# The kit's reference bot through the same in-process interface (sanity check)
python tools/eval.py --bot example --matches 48 --house-vision vision

# Machine-readable results + one JSONL replay frame trace per match
python tools/eval.py --matches 48 --json-out results.json --dump replay.jsonl
```

Useful flags: `--matches N`, `--seed S` (match i uses `S + i*10007`, so a seed set is stable),
`--duration SECONDS`, `--bot main|example`, `--bot-dir PATH`, `--house-vision pings|vision`,
repeatable `--knob dotted.path=value` (applied to a copy of `hello.config` before the bot and
match are built), repeatable `--house-knob key=value` (house skill; keys in
`tools/sim/house.py` `DEFAULT_SKILL`), `--variant no-fire|no-boost|no-loot|no-farm`, `--quiet`,
`--allow-errors`, `--json-out FILE`, `--dump FILE`.

The per-match line prints seed, place, score, match time, end reason, end HP, kills, damage
dealt/taken, zone damage and time outside the zone, cubes and decision latency. The summary adds
mean score/place, placement histogram, survival mean/median, damage totals, decision-time
mean/max and the number of decisions over the 50 ms budget.

Throughput on this box: **~0.7-1.4 s per 480 s match, 35-66 s for 48 matches on one core**, inside
the 90 s target and far inside the 15-minute budget. `--quick` is roughly 4 matches x <=150 s.

## Scoring (same rule as the track)

Placement is survival order: the first mech to die gets place 16, the last survivor gets place 1.
A mech that dies with `k` mechs alive at the start of that tick gets place `k`; simultaneous
deaths share that place. Score is `16 - place`; the summary reports the mean over matches.
At the 480 s timeout the surviving mechs are ranked by HP. A match stops as soon as the player
mech dies because that fixes the score; the remaining houses do not need simulating.

## What is modelled

* **Clock**: 20 Hz physics, decisions every 2 ticks (10 Hz), action applied for the two ticks
  until the next decision. Actions are dicts in the protocol shape (missing fields default; the
  bot's `act` return is applied exactly like `mf.run`).
* **Mechs**: 1400 HP, `mech` config numbers from the recorded hello (walk/boost speed, ground/air
  accel, gravity, jump/jet, step-up, energy, turn/pitch rates, regen delay/rate, rear multiplier,
  armor, charge move multiplier, self-damage multiplier). Cylinder radius/height, eye/muzzle
  heights and cube growth are used for hit tests and obs.
* **Movement**: 2.5-D on the map's baked walkability grid - ground speed, boost (and its drain),
  air control, jumping, jet ascent, wall slide, step-up, map bounds, mech push-apart. Aim pans at
  `turn_rate`/`pitch_rate` and clamps to `min_pitch`/`max_pitch`; firing uses the actual aim.
* **Weapons** (all four, stats from `hello.config.weapons`):
  * gun - 45 dmg bullets at 252 m/s, 0.24 s cooldown, 12-round magazine, 2.4 s reload,
    Gaussian spread, range 230 m;
  * mortar - ballistic shell, gravity 22, direct + splash falloff, knockback/knock-up, energy
    drain to victims, self-splash at 50%, cooldown 2.6 s;
  * laser - hold-to-charge, release-to-fire hitscan, damage scales 35%-100% with charge,
    `min_charge`/`hold_s`/`cookoff` handling, 280 m range, cooldown 1.65 s, `short` from lightning;
  * lightning - instant nearest target in a 30 degree cone within 45 m, 2 chains at 14 m with
    0.7 falloff, 3 energy per shot, 0.25 s cooldown.
  Damage goes through rear-arc x1.5, armor, cube damage scaling, and the kill/death pipeline.
  Buildings (all 259 prisms with their real `base`/`top`) and terrain block projectiles and beams;
  bridges are shoot/walk-under. Trees block sight, never shots (per protocol).
* **Zone**: the exact six-phase schedule (560 -> 380 -> 240 -> 140 -> 70 -> 25 -> 0, dps
  20/35/60/100/160/260), linear centre+radius interpolation inside each shrink, and the next
  circle sampled uniformly inside the current one. Zone damage, `in_zone`, `next_center`,
  `next_radius`, `shrink_start`/`shrink_end` are exposed in every obs.
* **Cubes, upgrades, offers**: cubes come from kills (cubes + 2), crates and supply drops;
  pickup radius 3.5 m horizontally and 4 m vertically; +4% max HP/damage up to 10 cubes, +5%
  size per cube; offer at the recorded thresholds with 3 choices, 5 s expiry defaulting to
  choice 0, max stacks and weapon `requires` enforced; all 22 upgrade effects from the recorded
  config are applied to `mech.*` / `weapons.<kind>.*` stats. Upgrades are per mech, so weapon
  upgrades survive dropping and re-picking that weapon kind.
* **Crates, loot, supply drops**: 36 crates (300 HP, drop a cube and a weapon with the configured
  50% chance), 40 floor weapon pickups with the protocol weights (mortar/laser/lightning 1:1:1),
  take/swap/drop semantics including cooldown floor and the no-drop-while-charging laser rule.
  Three supply drops called at 70/140/210 s, landing 25 s later: a sole mech inside the ring for
  4 s gets 3 cubes + full HP/energy; two or more freeze the clock; leaving restarts it.
* **NPC drones**: 12 per match, 350 HP, wander around a leash point, aggro anything within 45 m
  with line of sight (or whoever attacks them), chase up to the leash limit, fire 25-damage
  bullets at 90 m/s once a second, drop 2 cubes, take no zone damage.
* **Vision and sound**: eye at `pos.z + eye_height`; a target is visible when it is inside the
  45 m proximity ring (buildings but not canopy block) or inside the 120 degree yaw cone within
  200 m with 2-D line of sight through the map's sight grid (buildings + tree trunks). Seen
  mechs/NPCs stay in the obs for `vision.memory_s` seconds at their current position. Enemies'
  visible projectiles, crates, cubes, pickups and beams are reported like the server does.
  Sounds use the protocol carry ranges, near/mid/far distance buckets, world-yaw bearings with
  configurable Gaussian noise and an age; own sounds are not echoed.
* **House bots**: 15 scripted bots, 4 easy / 4 medium / 4 hard / 3 expert, with shared sensing,
  no friendly fire, starting secondaries by difficulty, weapon choice (lightning/laser/mortar/gun),
  strafing/orbiting at a preferred range, per-level aim error, reaction delay and engagement range.
  They never attack each other. Their skill is a set of explicit knobs (aim-error sigma, fire
  discipline, burst discipline, engagement/preferred range, convergence) whose defaults are
  calibrated against the real practice scores; see "House skill knobs" below. Two sensing models
  are available (`--house-vision`), because the track only says they "share vision, prefer the
  weakest mech":
  * `pings` (CLI default, kept for continuity): a shared noisy "where is the player" track
    refreshing every 6/4/2.5/1.5 s with 70/45/25/12 m of error, plus the legacy 6 s memory leak.
    Over-informed: breaking line of sight never works.
  * `vision` (the calibrated reference): union of what any living house actually sees (45 m ring /
    120 deg cone / 200 m / buildings block), shared for 3 s after the last sighting, no pings and
    no omniscience. This is structurally the closer model and the one the calibration targets.
  "Prefer the weakest" is moot in this solo track: all 15 houses have exactly one player mech to
  hunt, so the skill knobs and the shared-sensing model are the fidelity levers.
* **Determinism**: everything (spawns, zones, loot, aim noise) is drawn from a `random.Random`
  seeded by the match seed. The same seed and config reproduce the same match; no wall-clock
  randomness and no imports of `bot/` state leaks between matches (the baked world is cached
  read-only).

## Fidelity caveats (read before trusting an absolute number)

* **Absolute scores are calibrated, not ground truth.** The default house-skill levels were tuned
  so two independent anchors line up: the kit's `example_bot.py` (real practice score ~1.15)
  scores **1.19** here and the real bot at commit `96478d5` (real **2.85**) scores **2.85** under
  `--house-vision vision` over 48 seed-1 matches. At `866bf59` the real score is 3.04 and this
  simulator gives **2.60**; the 0.4 gap is inside the 48-match noise (per-match sd ~3.9, so the
  standard error of a 48-match mean is ~0.56). Absolute numbers are only meaningful with this
  calibrated default set and the matching seed set; they will drift if `bot/**` or the house
  knobs change. Re-run the two anchors after either changes.
* **The house-bot AI is invented.** We do not have the server's house code. The skill knobs
  (below) are the fidelity levers; the defaults reproduce the two anchors above, but the real
  houses may distribute skill differently across the 15 bots. Treat differences of less than
  ~one place (~0.3 score) as noise, and prefer paired seeds for A/B. The `pings` sensing model is
  ~+0.3 score more generous than `vision` (calibrated reference), so quote `vision` numbers when
  comparing with the real ladder.
* **Movement is not a full physics clone.** The map walkability grid is `bot/world.py`'s 2 m grid;
  slopes, roof edges, overhang undersides and fine step-up geometry are approximated, and roofs
  are not traversed. Jet/gravity/boost use the protocol constants but not the server solver's
  collision resolution order. Wall-heavy fights can therefore differ.
* **Vision is 2-D.** The protocol allows line of sight to a target's centre *or* top and has a
  soft canopy term; the sim uses a 2-D sight grid (prisms whose vertical span covers the eye plus
  tree trunks) and ignores the canopy density threshold. This mostly matters for peeking over
  low cover; it is uniform across bot variants, so A/B conclusions are not affected.
* **Roof loot is not spawned.** A quarter of the server's crates and floor pickups are on
  rooftops; the bot cannot reach them and does not model 3-D roof movement, so they are omitted
  (see `# TODO(roofs)` in `tools/sim/match.py`). Matches have slightly less loot than real ones.
* **Some server details are approximated**: mech-vs-mech push (simple separation), projectile
  radii vs cylinders, explosion occlusion by buildings, "two mechs take the same pickup" tie
  breaking, exact sound occlusion, and the precise event cadence of zone damage. None of these
  change the macro game (who sees whom, who dies first).
* **NPC drones are simplified**: wander/leash/aggro are followed, but pathing is a straight-line
  steer with wall slide rather than a navmesh, and they do not lead shots. They still kill
  distracted houses (a real effect visible in the recorded stream: 3 mechs were already dead by
  t=36.8 with only one house in the player's view).

## Calibration / reference numbers (seed 1, 48 matches, one core)

The house-skill defaults were calibrated on 2026-11-21 against the two independent real practice
scores we have. The reference command is
`python tools/eval.py --matches 48 --seed 1 --house-vision vision` (add
`--bot-dir .reports/bot_<commit>/bot` to score an exported older commit without touching `bot/`).

```
bot under test          real practice   sim vision     sim pings    vision place / survival
kit example_bot.py          ~1.15          1.188          1.458       14.81 / 140.9 s
bot @ 96478d5                2.85          2.854          2.562       13.15 / 143.8 s
bot @ 866bf59                3.04          2.604            --        13.40 / 122.8 s
bot @ 8988bf0 (HEAD)          --           3.000            --        13.00 / 167.0 s
```

* `96478d5` is the primary anchor: real 2.85 vs simulated 2.854. The older commit `866bf59`
  lands 0.44 low; that is inside the 48-match standard error (~0.56), and the two commits are
  within noise of each other in the sim (2.60 vs 2.85) even though the real ladder separates them
  by 0.19.
* The example bot's real ~1.15 is matched to 0.04 under `vision`; under the `pings` sensing model
  it is +0.27 high. Quote `vision` numbers when comparing with the real ladder.
* Wall-clock for one 48-match cell: 35.6 s (example), 66.0 s (current bot) on this box, under the
  90 s budget. Bot decisions: mean 0.30 ms, max 7.3 ms, 0/48 over the 50 ms budget, `bot_fails` 0.
* Before this calibration (the old house levels in the previous version of this file) the same
  cells read example 1.42 / current bot 4.90 under vision. Those absolute numbers are obsolete;
  only runs made with the current defaults are comparable to the table above.

The bot is still bimodal against a coordinated hunt: at `96478d5` under vision it is 16th in 11
of 48 matches and 15th in 13 more, with zero wins (best place 3); mean cubes 0.0 because it
rarely survives long enough to loot. Early-game survivability, not the late game, is the
bottleneck — consistent with the earlier, uncalibrated observation.

### House skill knobs (calibrated defaults)

`tools/sim/house.py` `LEVELS` defines, per difficulty 1-4 (easy, medium, hard, expert):

| knob | easy | medium | hard | expert | meaning |
|---|---|---|---|---|---|
| `aim_sigma_deg` | 4.42 | 2.72 | 1.53 | 0.85 | Gaussian aim error added to every shot (degrees, 1-sigma) |
| `fire_tol_deg` | 4.0 | 2.8 | 1.8 | 1.1 | only fire when the torso is within this angle of the solution |
| `burst_s` / `burst_pause_s` | 99 / 0 | 99 / 0 | 99 / 0 | 99 / 0 | gun burst length / pause (99 = continuous fire, disabled by default) |
| `engage_m` | 130 | 165 | 195 | 215 | max range at which the house will shoot at the player |
| `pref_m` | 85 | 95 | 105 | 115 | preferred orbit distance (smaller = closes in harder) |
| `react` | 0.9 | 0.55 | 0.3 | 0.15 | seconds between first sighting and first shot |
| `secondary` | 0.15 | 0.35 | 0.60 | 0.85 | chance of starting with a looted secondary |
| `strafe` | 2.2 | 1.8 | 1.5 | 1.2 | seconds between strafe-direction flips |
| `converge` | 1.0 | 1.0 | 1.0 | 1.0 | movement-speed factor while chasing the shared player track |
| `converge_boost` | 1 | 1 | 1 | 1 | boost toward a shared sighting when farther than 60 m (energy permitting) |

Override at run time with repeated `--house-knob key=value` (applied to every level; values are
floats, so `--house-knob aim_sigma_deg=3.0 --house-knob converge_boost=0`). The full key list is
`tools/sim/house.py` `DEFAULT_SKILL`; unknown keys are rejected by the CLI. Knobs are recorded in
the JSON payload (`house_skill`) so a calibration run is reproducible.

What moved the score (48 seed-1 matches, `--house-vision vision`, HEAD `96478d5`):

```
aim_sigma scale (relative to the pre-calibration 2.6/1.6/0.9/0.5 degrees)
1.0 (tight)   bot ~1.6     example ~1.06      houses snipe; both bots die fast
1.7 (default) bot  2.85    example  1.19      calibrated point
1.8-1.9       bot  2.6-3.4 example  1.1-1.4   (48-match noise ~±0.6)
2.2+ (loose)  bot  3.5-4.1 example ~1.4       houses spray; bot survives longer
```

* **`aim_sigma_deg` is by far the strongest lever** for this bot: loosening it from the
  pre-calibration value to the calibrated one moved the current-bot score by ~1.5-2 points and
  changed the example bot very little (its deaths are ~60% zone, not house fire).
* **`converge_boost`** is the second lever (~0.4-0.5 score in a 16-match screen): with boosting
  on, the house team arrives at a shared sighting faster, which raises pressure on a mobile bot.
* **`engage_m`/`pref_m`** shift the fight distance by ~0.2-0.3 score; **`react`/`secondary`** are
  minor once sigma is set; **burst discipline** is a no-op at its default (99 s = continuous) and
  only becomes useful if the real houses are modelled as burst-limited.


### House sensing: pings vs vision

`--house-vision {pings,vision}` (default `pings`, so the numbers above and older runs stay
comparable) selects how the scripted houses know where the player is:

* **pings** (original approximation): houses share a noisy "where is the player" track that
  refreshes every 6/4/2.5/1.5 s with 70/45/25/12 m of error (keyed to the highest house level
  still alive), plus the legacy 6 s memory leak. This models "the server houses share sight and
  hunt the weakest mech" but over-informs them: breaking line of sight never works.
* **vision** (closer to the stated rule): a house sees the player only through its own alive
  vision (45 m proximity ring or 120 deg cone within 200 m, buildings block) and every living
  house shares what any one of them currently sees (union of real sightings). The shared picture
  is forgotten 3 s after the last sighting; no pings. Houses still never damage each other.

Wiring: `tools/sim/match.py` `HOUSE_PING` (line 33), `Match.__init__(..., house_vision=...)`
(line 53), `Match.house_think` (line 1119), shared memory `self.house_shared_memory = 3.0`
(line 61); `tools/eval.py --house-vision`; diagnostics in `Match.finish()` (`house_sight_avg`,
`house_sight_max`, `house_sight_ever`, `house_pings`, `house_known_frac`, `bot_fails`).

Measured cost of the model choice under the calibrated defaults (48 matches, seed 1, same seeds):

```
example bot     pings 1.458 -> vision 1.188
bot @ 96478d5   pings 2.562 -> vision 2.854
```

The example bot's real ~1.15 is matched only by `vision`; `pings` is +0.27 high. For the current
bot the direction reversed after calibration: `pings` is 0.29 *lower* than vision. With calibrated
skill the house team's problem is not finding the player (a noisy shared track is enough) but
aiming at it, and `vision` supplies an exact current position whenever any house has line of
sight; the noisy pings track only tells the team roughly where to run. `vision` remains the
structurally faithful model and the calibration reference; quote it. Tuning conclusions about
aggression/cover should be re-checked under it, because under `pings` breaking line of sight
cannot shake the house team.

Sanity probe (`tools/sim/probe_house_vision.py`, a bot that stands still, 8 seed-1 matches,
calibrated defaults):

```
pings   mean survival  64.4 s (median  61.9, max 130.9), 1.13 houses seeing, 28.2 pings/match
vision  mean survival 114.6 s (median 139.2, max 204.6), 0.69 houses seeing,  0.0 pings/match
```

Vision genuinely gates the houses (no pings, fewer simultaneous sightings); the standing target
survives *longer* under vision because the whole house team no longer converges immediately on a
pinged position. Both modes kill a standing target in every match (place 16), so this probe shows
the sensing difference, not an escape strategy.


## How to interpret A/B results

* Keep the seed set identical (`--seed`), run at least 24-48 matches, and compare the mean score
  and the placement histogram, not a single match. With the calibrated defaults a 48-match mean
  has a standard error of roughly 0.5-0.6 score, so an unpaired difference below ~0.5 is noise;
  paired seeds (same seed set for both variants) are much tighter and can resolve ~0.25.
* Absolute-score checks: after any change to `bot/**` or to the house knobs, re-run the two
  calibration anchors (`example_bot.py` -> ~1.19, exported `96478d5` -> ~2.85 under
  `--house-vision vision`) before quoting an absolute number. If they move, the simulator's
  absolute scale moved, not the bot.
* The simulator is most trustworthy for: weapon/engagement-range policy changes, zone rotation,
  loot timing, target selection, and (somewhat) hit-rate changes from aim/lead logic.
* It is least trustworthy for: exact movement tricks (roof climbing, wall-sliding speed), the
  effect of a single-shot kill on a specific house difficulty, and anything that depends on the
  server's hidden house AI.
* `--variant no-fire` is the negative control (mean 0.125 on 8 seed-1 matches); a candidate
  change should move the score by more than roughly one place (~0.25-0.4 points) to be believable.

## File map

| file | role |
| --- | --- |
| `tools/eval.py` | CLI, per-match loop, aggregation, JSON/replay output |
| `tools/sim/kitcfg.py` | recorded hello/config, per-match hello with knob overrides |
| `tools/sim/loader.py` | loads `bot/main.py` and the kit example bot in-process, caches the baked world |
| `tools/sim/match.py` | match state, spawns, obs building, ticks, scoring, house orchestration |
| `tools/sim/entities.py` | mech/NPC/projectile/weapon state, cubes, upgrades, offer data |
| `tools/sim/combat.py` | firing, projectiles, hitscan lasers, lightning, explosions, damage |
| `tools/sim/arena.py` | world.json prisms, 3-D ray casts against prisms/terrain, cylinder hits |
| `tools/sim/zone.py` | zone schedule, circle sampling, in-zone tests |
| `tools/sim/house.py` | scripted house-bot AI and difficulty levels |
| `tools/sim/probe_house_vision.py` | standing-target probe: pings vs vision house sensing |

All files are stdlib-only (`math`, `random`, `json`, `gzip`, `time`, `argparse`, ...). Nothing
under `bot/` or `kit/` is modified; the simulator imports them read-only.
