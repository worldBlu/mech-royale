# Offline match simulator (`tools/sim/`)

A deterministic, standard-library-only approximation of a Mech Royale match, built to answer
one question: **does bot change A score better than bot change B, all else equal?**
It is not the official scorer. The official server has the real house-bot AI, the real movement
solver and the real 16-player matchmaking; this simulator approximates all three and documents
where it differs.

## Run it

From the repo root (a Python 3.12 interpreter is enough; nothing outside the stdlib is used):

```powershell
# 48 practice matches of bot/main.py (the scoring command)
python tools/eval.py --matches 48

# Fast loop while tuning (4 matches, 150 s cap per match)
python tools/eval.py --quick

# A/B two policy variants on the same seeds
python tools/eval.py --matches 24 --seed 7 --variant no-fire
python tools/eval.py --matches 24 --seed 7 --knob vision.range=120

# The kit's reference bot through the same in-process interface (sanity check)
python tools/eval.py --bot example --matches 48

# Machine-readable results + one JSONL replay frame trace per match
python tools/eval.py --matches 48 --json-out results.json --dump replay.jsonl
```

Useful flags: `--matches N`, `--seed S` (match i uses `S + i*10007`, so a seed set is stable),
`--duration SECONDS`, `--bot main|example`, repeatable `--knob dotted.path=value` (applied to a
copy of `hello.config` before the bot and match are built), `--variant no-fire|no-boost|no-loot|no-farm`,
`--quiet`, `--allow-errors`, `--json-out FILE`, `--dump FILE`.

The per-match line prints seed, place, score, match time, end reason, end HP, kills, damage
dealt/taken, zone damage and time outside the zone, cubes and decision latency. The summary adds
mean score/place, placement histogram, survival mean/median, damage totals, decision-time
mean/max and the number of decisions over the 50 ms budget.

Throughput on this box: **~1.0-1.3 s per 480 s match, ~51 s for 48 matches on one core**, far
inside the 15-minute budget. `--quick` is roughly 4 matches x <=150 s.

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
  They never attack each other. Two sensing models are available (`--house-vision`), because the
  track only says they "share vision, prefer the weakest mech":
  * `pings` (default): a shared noisy "where is the player" track refreshing every 6/4/2.5/1.5 s
    with 70/45/25/12 m of error, plus the legacy 6 s memory leak. Over-informed, but keeps older
    calibration numbers comparable.
  * `vision`: union of what any living house actually sees (45 m ring / 120 deg cone / 200 m /
    buildings block), shared for 3 s after the last sighting, no pings and no omniscience. This
    is structurally the closer model. See the calibration section for the cost of the choice.
  "Prefer the weakest" is moot in this solo track: all 15 houses have exactly one player mech to
  hunt, so the pings noise table / vision union are the fidelity levers.
* **Determinism**: everything (spawns, zones, loot, aim noise) is drawn from a `random.Random`
  seeded by the match seed. The same seed and config reproduce the same match; no wall-clock
  randomness and no imports of `bot/` state leaks between matches (the baked world is cached
  read-only).

## Fidelity caveats (read before trusting an absolute number)

* **Absolute scores are optimistic.** Calibration against the only independent number we have:
  the kit's `example_bot.py` scores a reported ~1.15 on the track. In this simulator it means
  **1.90 under `pings`** and **1.42 under `--house-vision vision`** over 48 matches, so the
  default pings model is roughly **+0.5 to +0.8 score generous** for a passive bot, while the
  vision model is only ~+0.3 generous. Use paired seeds to compare variants, not absolute values.
* **The house-bot AI is invented.** We do not have the server's house code. Aim error, reaction,
  engagement and the shared-sensing model are tuned so the example bot's survival sits in the
  right ballpark. If the real houses are less coordinated, this sim is harsher; if they hunt
  harder, it is too kind. `HOUSE_PING` in `tools/sim/match.py` and `--house-vision vision` are
  the knobs; prefer paired runs under both when a tuning decision is close.
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

```
house sensing   bot under test   mean score  mean place  mean survival
pings (default) example bot           1.896      14.104        163.3 s
vision          example bot           1.417      14.583        151.2 s
pings (default) current bot           4.625      11.375        162.7 s
vision          current bot           3.646      12.354        172.8 s
```


The current bot row is a moving target: it was measured at `bot/policy.py` hash `68D4EF81FD87`
(mtime 15:15, uncommitted work by the Lead, git HEAD `a3fd32d`). Earlier the same bot scored
2.33 (a pre-cover/AI version); re-run the cell after `bot/**` settles if a number will be quoted.

Bimodality persists under the pings model: place 16 or 15 in 22 of 48 matches, but 5 wins where
it snowballs to 16-29 cubes and 1900+ HP. Mean cubes 1.65. Its early-game survivability against
a coordinated hunt is the clear bottleneck; navigation/zone discipline is fine (4.2 s average
outside the zone, 304 zone damage). Decision latency in the final 48-match run was mean 0.2 ms /
max 6.2 ms with 0 decisions over the 50 ms budget; `bot_fails` was 0.

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

Measured cost of the model choice (48 matches, seed 1, same seeds):

```
example bot  pings 1.896 -> vision 1.417   (paired vision-pings -0.479, se 0.218)
current bot  pings 4.625 -> vision 3.646   (paired vision-pings -0.250, se 0.697)
```

The example bot's real reported score is ~1.15, so the **vision model is closer to the real
track**; under vision the current bot's win count drops 5 -> 1 and its place histogram compresses
toward the middle (place 16: 10 -> 7, place 15: 12 -> 9, place 12: 2 -> 8), i.e. it dies less
first but also stops snowballing. Tuning conclusions about aggression/cover should be re-checked
under `--house-vision vision`; the pings model makes fleeing pointless by construction.

Sanity probe (`tools/sim/probe_house_vision.py`, a bot that stands still, 8 seed-1 matches):

```
pings   mean survival 120.0 s (median 139.1, max 235.7), 1.05 houses seeing, 59.8 pings/match
vision  mean survival 159.8 s (median 188.4, max 208.8), 0.57 houses seeing,  0.0 pings/match
```

Vision genuinely gates the houses (no pings, fewer simultaneous sightings); the standing target
survives *longer* under vision because, with omniscient pings, the whole house team converges on
it immediately.


## How to interpret A/B results

* Keep the seed set identical (`--seed`), run at least 24-48 matches, and compare the mean score
  and the placement histogram, not a single match.
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
