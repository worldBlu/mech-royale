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
* **House bots**: 15 scripted bots, 4 easy / 4 medium / 4 hard / 3 expert, with a shared vision
  pool, no friendly fire, starting secondaries by difficulty, weapon choice (lightning/laser/
  mortar/gun), strafing/orbiting at a preferred range, per-level aim error, reaction delay and
  engagement range. They never attack each other. Because the track notes say the houses prefer
  the weakest player mech, they also share a noisy "where is the player" track that refreshes
  every 6/4/2.5/1.5 s with 70/45/25/12 m of error (highest difficulty still alive). That single
  approximation is what makes the houses actually hunt the bot instead of idling at the next
  circle; it is the biggest fidelity lever in the simulator.
* **Determinism**: everything (spawns, zones, loot, aim noise) is drawn from a `random.Random`
  seeded by the match seed. The same seed and config reproduce the same match; no wall-clock
  randomness and no imports of `bot/` state leaks between matches (the baked world is cached
  read-only).

## Fidelity caveats (read before trusting an absolute number)

* **Absolute scores are optimistic.** Calibration against the only independent number we have:
  the kit's `example_bot.py` scores a reported ~1.15 on the track. In this simulator its mean is
  **1.90 over 48 matches** (places: 22x16, 7x15, 4x14, ... mean place 14.10) with the same house
  pressure as the real bot. So the simulator is roughly **+0.5 to +0.8 score generous** for a
  passive bot; our current bot's 2.33 is best read as "a bit better than the example bot", not
  as a prediction of the ladder score. Use paired seeds to compare variants, not absolute values.
* **The house-bot AI is invented.** We do not have the server's house code. Aim error, reaction,
  engagement and the shared player track are tuned so the example bot's survival sits in the
  right ballpark. If the real houses are less coordinated, this sim is harsher; if they hunt
  harder, it is too kind. The `HOUSE_PING` table in `tools/sim/match.py` is the knob.
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
example bot  mean score 1.896 (sd 2.47)  mean place 14.10  survival 163.3 s  zone dmg 622
current bot  mean score 2.333 (sd 3.97)  mean place 13.67  survival 114.3 s  zone dmg 97
```

The current bot is bimodal: 17x place 16 and 16x place 15 (first or second to die, 25 of 48
matches end under 90 s, often with 0 cubes) but also 3 wins where it snowballs to 16-29 cubes
and 1900+ HP. Its early-game survivability against a coordinated hunt is the clear bottleneck;
navigation/zone discipline is fine (2.2 s average outside the zone, 97 zone damage). Decision
latency in the final 48-match run was mean 0.35 ms / max 11.9 ms with 0 decisions over the
50 ms budget (an earlier run against a pre-optimisation `bot/` had a 112.6 ms outlier, so keep
an eye on the tail as policy code grows).

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

All files are stdlib-only (`math`, `random`, `json`, `gzip`, `time`, `argparse`, ...). Nothing
under `bot/` or `kit/` is modified; the simulator imports them read-only.
