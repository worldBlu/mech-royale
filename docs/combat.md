# Combat: aim solving, fire gating and measured accuracy

This is the design note for the combat layer (`bot/combat.py`), the replay harness used to
measure it (`tools/replay.py`) and the synthetic test suite (`tests/test_combat.py`).
`bot/perceive.py` was left as-is.

## TL;DR

- Shots leave along the torso's **actual** aim (`obs.self.aim`), so the solver computes a
  desired direction from the target's *current* geometry and the trigger is gated on the
  **current** aim error converted to metres at the target.
- Gun: fixed-point lead from the 3 m shot origin; laser: hitscan, no lead, release only
  near full charge; mortar: exact low-angle ballistic solution (including the tube 2.5 m
  above the gun muzzle) with an iterated flight-time lead; lightning: gate the 30 degree
  cone, not metre-level aim.
- A target the server currently lists in `obs.mechs` is visible now; its line is treated as
  clear for shooting even when the world grid says "tree", because trees block vision but
  not bullets/lasers.
- Measured on `kit/check/stream.jsonl.gz` with the current policy: gun 16/27 shots (59.3%,
  720 damage, 45/hit); laser arm 8 shots / 7 hits (87.5%, 2205 damage, 315/hit); mortar arm
  3 shots / 0 hits (2 "untracked", 1 tracked miss at 9.97 m surface distance; 0 damage);
  lightning never had a target inside 45 m. The mortar is behind a `use_mortar` knob so the
  Lead can A/B it in the real match without a code change.

## Public API (kept stable for `bot/policy.py`)

`Combat(world, cfg)` exposes:

| member | meaning |
| --- | --- |
| `pick(me, d, los, tr=None)` | best weapon slot to use against this target, or `None` |
| `aim_for(me, tr, kind, d, now=None)` | desired `[yaw, pitch]`; `now` extrapolates a stale track |
| `decide(obs, tr, now)` | `{"aim": [...], "fire": [bool, bool], "reload": bool?}` |
| `holding(me, kind)` | slot index holding `kind`, or `None` |
| `can_hit(kind, d, los)` | coarse range/line check used by `pick` |
| `last_kind` | weapon chosen by the last `decide` (used for positioning) |
| `charging` | true while the laser is charging (policy suppresses boost) |
| `use_mortar` | A/B knob, default `True`; config `combat.use_mortar=0` disables the mortar |

`fire` is always exactly two booleans (schema `minItems`/`maxItems` 2). When only `aim`
matters, the key can be omitted (missing fields default to "no trigger").

## Aim solver

World conventions from `kit/protocol.md`: `yaw = atan2(dy, dx)` around +z, pitch is
positive up, and `dir = [cos(pitch)cos(yaw), cos(pitch)sin(yaw), sin(pitch)]`.
Mech position is the feet; the target centre is `z + 4.5` (body height 9) and for an NPC
`z + 1.75` (height 3.5). Cubes scale the mech, so the centre is moved up by the same scale.
The gun muzzle is `pos.z + 7`; the mortar tube is `pos.z + 9.5` (gun muzzle + 2.5). All
muzzle heights, speeds, cooldowns, charge times and pitch limits are read from the
per-observation `stats` (`self.stats` and `weapons[i].stats`), not frozen from `cfg`,
because upgrades change them.

Every shot spawns 3 m out along the aim direction. That does not change the direction for
a stationary target, but it shortens the projectile's flight; the gun's lead iteration and
the mortar's ballistic solve both subtract that 3 m.

### Gun (252 m/s, straight, 45 dmg, 0.6 degree spread)

Lead the target with a fixed-point iteration: start from the flight time of the current
distance, predict the target `p = t + v*t`, recompute `t = (|p - muzzle| - 3)/252`, repeat
three times. Fire only when the predicted impact is inside the body: 2.1 m lateral and
3.6 m vertical margin (body radius 2.5, half height 4.5).

### Laser (hitscan after charge)

No lead: the beam is instantaneous, so the desired direction is the target's current
centre. The torso keeps charging while it slews. Release (send `fire[slot] = False`) only
at `>= 96%` of max charge (`0.72 s` of `0.75`) *and* while aligned (LOS, range, pitch
limits, 2.0 m lateral, 3.5 m vertical). Below that the trigger stays held. Releasing below
`min_charge` cannot cancel a charge - the server still fires at 0.15 s - so the code never
deliberately releases early.

The protocol laser numbers, verified from `kit/protocol.md`:

- charge time `0.75 s`; damage `315 * (0.35 + 0.65 * charge/0.75)`, i.e. 35% at zero charge
  / `min_frac` and 100% at full;
- `min_charge 0.15 s` (a release before it still fires as soon as 0.15 s is reached),
  then `cooldown 1.65 s`; a full charge held `hold_s 2 s` longer auto-fires;
- **cook-off is a +60% damage bonus against a mech that is holding a mortar** (it is a
  property of the target, not a risk to the laser user);
- charge is reported in the observation as seconds charged, quantised by the 20 Hz tick
  (`0.05 s`). Decisions happen every `0.1 s`, so a release threshold of `0.72 s` is crossed
  at the observed `0.75 s` - the replay confirms every aimed laser shot was fired at
  `charge = 0.75`, damage `315`.

Damage and cycle DPS:

| release charge | damage | DPS (charge + 1.65 s cooldown) |
| --- | --- | --- |
| 0.62 s (old code's constant, if released there) | 279.5 | 123.1 |
| 0.70 s (first 0.1 s observation at/above 0.62) | 301.4 | 128.3 |
| 0.75 s (full) | 315.0 | 131.3 |

The old constant was therefore throwing away 4-11% of the shot (not 17% - damage is a
linear ramp in charge, and the old release was observed one or two 0.1 s samples after
0.62, not at 0.62), and full charge is also the best sustained DPS. The code already used
`LASER_RELEASE_FRAC = 0.96` before this task; the replay measurement now confirms the
actual release is full charge and no change was needed.

### Mortar (72 m/s, gravity 22, 200 direct + 160 splash)

The old code used a fixed 0.25 s lead and a hand-tuned height correction; at 100 m the
shell flies ~1.5 s, so the lead was wrong by an order of magnitude. The new solver:

1. predict the target at the flight time;
2. solve the low-angle launch pitch that crosses `x` metres ahead and `dz` above the tube:
   `a = g*x^2/(2*v^2)`, `u = tan(pitch) = (x - sqrt(x^2 - 4*a*(dz + a))) / (2*a)`;
   if the discriminant is negative the point is unreachable, and the code refuses to fire;
3. iterate the lead (4 passes) because the flight time depends on the pitch.

The 3 m shot-origin offset is folded into the solve (the effective start point is
`x - 3*cos(pitch)`, `dz - 3*sin(pitch)`). A static flat-ground check gives -12.5 deg at
20 m, 8.8 deg at 100 m and 25.9 deg at 200 m, and the analytic trajectory passes through
the target at the requested horizontal distance.

The fire gate converts pitch error into range error through `d(range)/d(pitch) =
2*v^2/g * cos(2*pitch)` and accepts up to 5.5 m; with a 7 m splash a tracked miss under
~5 m should still damage. The minimum mortar range is 20 m and `pick` only chooses it when
the target is out of LOS or farther than 55 m, to avoid self-splash.

**Measured on the recording (replay instrumentation).** The three mortar shots in the
stream are all long-flight: 95-145 m, 1.3-2.2 s time of flight. `tools/replay.py` now
reports a signed impact error for every shell (`along_m` > 0 = long, `lateral_m`, `dz_m`,
`splash_m`, `direct`, `tof_s`) plus splash/direct damage. For the only shot where the
target is still tracked at impact (`t=22.95`, 95 m, 1.62 s flight):

- the bot's solve put the shell within 0.2 m of the target centre at the target's
  fire-time distance, so the ballistic pitch and lead were right at fire time;
- 0.15 s after the shot the target started a jetpack climb at `+8.9 m/s` and rose 13 m
  over the flight, while changing course every 0.1 s. The shell therefore impacted
  12.7 m long, 8.4 m lateral and 19.2 m below the target's centre - no splash, no damage;
- the other two shots are "untracked": the target left the recording before impact, so
  there is no evidence they missed for aim reasons (one passed 3.6 m below the target's
  fire-time centre at its horizontal range).

Conclusion: no systematic solve bias was found (aim error p50 0.36 deg, shell passes the
aimed point); the miss is future acceleration and terrain height that a 1.3-2.2 s ballistic
lead cannot know. The mortar is therefore a low-confidence weapon against moving mechs, and
the fix is target selection, not more aim math. `Combat.use_mortar` (constructor config
`combat.use_mortar`, default 1; replay `--no-mortar`) lets the Lead A/B it off. It is not
silently disabled: with it on the recording fires 3 shells for 0 damage; with it off the
gun result is identical (27/16) - the mortar shots are opportunistic shots while the gun is
on cooldown, so the knob is neutral in this stream and must be decided on real-match data.

### Lightning (30 degree cone, 45 m, 60 dmg)

Instant and automatic: it hits the nearest target inside a 30 degree cone around the aim
yaw, with LOS. The gate uses `0.95 * cone_deg = 28.5 deg` and costs 3 energy. The old
0.30 rad (17.2 deg) gate was much tighter than the actual 30 degree cone.

## Line of sight and weapon choice

`world.has_los` is a 2D grid that also marks tree-trunk cells as sight blockers. Trees in
Mech Royale stop vision but **not** projectiles, so a target that is currently in
`obs.mechs` is treated as shootable (`visible_now`), regardless of the grid. Only a
remembered target (not currently visible) falls back to the grid, and there the mortar is
preferred because it can lob over the cover.

`pick` order:

1. laser if ready and `can_hit`;
2. lightning if ready, in range and the target is within 0.95 rad of the current aim yaw;
3. gun if it has ammo, LOS and range (the reliable DPS);
4. mortar if `use_mortar`, ready, in range and (no LOS or target farther than 55 m);
5. an empty gun last, so `decide` asks for the reload.

## Fire gating

The trigger is only pulled when the shot that would actually leave now can hit. The errors
are measured between the desired aim and `me["aim"]` (the torso's actual aim, which slews
at `turn_rate` 4 rad/s / `pitch_rate` 3 rad/s):

- lateral miss = `horizontal_range * sin(yaw_error)`;
- vertical miss = `3D_range * sin(pitch_error)`;
- the desired pitch must be inside `[min_pitch, max_pitch]` (a target below the feet or
  above the torso cannot be hit);
- cooldown/ammo/reload state is checked per weapon;
- the laser is special: it holds the trigger while charging and only releases when aligned
  at full charge.

This is deliberately conservative: the current aim is an upper bound on the aim at impact
because the torso only turns toward the command. Firing into the torso motion is not
modeled.

## Replay harness (`tools/replay.py`)

Runs the real bot in-process over `kit/check/stream.jsonl.gz` (or any recorded stream),
feeding observations through `Bot.act`. It does **not** trust the recorded `obs.self.aim`
or weapon state - those belong to the original match - so it simulates:

- torso slew at `turn_rate`/`pitch_rate` per 0.05 s tick, pitch clamped to the mech limits;
- own gun ammo/reload/cooldown, laser charge/hold/auto-fire/release, mortar cooldown,
  lightning cooldown and energy (18/s regen after 0.05 s);
- two server ticks per decision (the action is applied for 0.1 s), controls first, shots
  after the slew for that tick;
- hit tests against the recorded target trajectories: gun bullets step 0.01 s against a
  mech/NPC cylinder with ground stops, mortar shells step 0.02 s with gravity 22 and 7 m
  splash, laser is a hitscan perpendicular-distance test, lightning picks the nearest
  target in range 45 and the 30 degree cone.

Usage:

```
python tools/replay.py                           # full summary
python tools/replay.py --trace                   # + one line per match second
python tools/replay.py --arm laser               # what-if: give us slot 1 = laser
python tools/replay.py --arm mortar --json out.json
python tools/replay.py --arm mortar --no-mortar  # A/B: combat.use_mortar = False
python tools/replay.py --no-sim                  # feed obs unchanged (no shots)
```

`--arm` injects a weapon into the empty second slot (the recording bot only ever had a
gun), so the other weapon paths can be exercised on the same geometry. `--no-sim` is the
raw feed: it measures the aim command against the recorded aim and does not simulate shots.

Reported diagnostics: policy mode split (decisions and simulated ticks), mode switches,
recorded horizontal path, decisions outside the zone, boost/jump/reload decisions, the
chosen combat target and its distance, currently-visible enemy distance, simulated shots /
hits / tracked misses / untracked shots per weapon, miss distance at impact, damage per
weapon (laser uses the protocol charge curve 315*(0.35+0.65*charge/0.75), mortar 200 direct
+ 160 splash falling to 30% at 7 m, gun 45, lightning 60), aim error at fire time (deg) and
the implied lateral miss (m), a signed mortar impact table (`along/lateral/dz/splash/direct/
tof`, positive along = long) and every protocol event with its source
(`HIT ... on <target> with <weapon> [REAR]`, `DAMAGED ... by mech <id>|zone ...`).

"Untracked" means the intended target is missing from the recording at the moment of
impact (it died or left the recording). Those shots are not counted as misses; the hit
percentage divides by `shots - untracked` so a stale target does not make the weapons look
worse than they are.

## Measurements

Recording: 300 observations, 36.8 s, 16 mechs, the self bot has only a gun. Exactly one
hostile (mech id 10) is ever visible; the combat target is at p50 140.1 m (p90 150.3 m),
and the recorded self path is 363.1 m with 0 decisions outside the zone. Policy split in
the current tree (after the Lead's policy changes): EVADE 169 decisions (56.3%), ROTATE 62
(20.7%), FIGHT 52 (17.3%), HEAL 16 (5.3%), FARM 1; 11 switches; boost on 229 decisions
(458 ticks); jump 28; reload 0.

### Before / after, same policy and same replay

| run | fire decisions | shots | hits | hit % | tracked miss p50 / p90 | aim error p50 / p90 |
| --- | --- | --- | --- | --- | --- | --- |
| old `combat.py` (HEAD `21ca44f`) | 9 | 6 | 3 | 50.0% | 2.21 / 6.70 m | 0.08 / 0.22 deg |
| new `combat.py` (policy at that time) | 31 | 31 | 18 | 58.1% | 1.46 / 6.81 m | 0.24 / 0.45 deg |
| current tree (policy changed by the Lead) | 27 | 27 | 16 | 59.3% | 1.46 / 6.81 m | 0.28 / 0.38 deg |

The new gate admits about `atan2(2.1, 140) = 0.86 deg` of lateral error instead of the old
`atan2(1.5, 140) = 0.61 deg`, which is why its average aim error at fire time is larger -
but it fires every cooldown instead of only on near-perfect aim, so it gets more shots and
a slightly better hit rate. The old code also ignored cooldown when requesting fire
(9 requests -> only 6 actual shots).

The older full-match baseline (old combat **and** old policy at HEAD) was 6 gun shots /
3 hits, modes ROTATE 165 / FIGHT 73 / EVADE 61 / FARM 1; the policy has since been changed
by the Lead, so only the A/B table above isolates the combat changes.

### Weapon what-ifs on the same recording

| arm | gun shots/hits | other weapon shots/hits | damage (weapon) | fire requests | other weapon aim error p50 / p90 |
| --- | --- | --- | --- | --- | --- |
| default (gun only) | 27 / 16 (59.3%) | - | 720 (gun) | gun 27 | - |
| `--arm laser` | 19 / 12 (63.2%) | laser 8 / 7 (87.5%) | 540 (gun) + 2205 (laser) | laser 67, gun 19 | 0.22 / 0.27 deg |
| `--arm mortar` | 27 / 16 | mortar 3 / 0 (1 tracked miss at 9.97 m, 2 untracked) | 720 (gun) + 0 (mortar) | gun 27, mortar 3 | 0.32 / 0.36 deg |
| `--arm lightning` | 27 / 16 | none (no hostile ever inside 45 m) | 720 (gun) | gun 27 | - |

Damage per hit: gun 45, laser 315 (every aimed laser shot was fired at `charge = 0.75`).
The laser's 8th shot was a 0.6-charge release with no target when the hostile left vision
and the policy stopped calling combat; the 7 aimed laser shots all hit. Laser fire requests
are high because holding the trigger while charging counts as a fire decision every 0.1 s;
actual shots are limited by the 0.75 s charge and 1.65 s cooldown.

## What still misses, and why

- **Gun at 140 m (59.3%).** Flight time is 0.5-0.56 s. The recorded target changes velocity
  every 0.1 s (id 10's `vx` moves between -12.4 and -5.9 m/s inside 0.5 s), so a linear lead
  cannot predict it: a standalone study over all visible mech samples measured the
  horizontal lead error at p50 1.90 m / mean 2.69 m. Simulated miss distances at impact are
  p50 1.46 m, p90 6.81 m, max 11.12 m - pure target jink, not aim math. On top of that the
  hit simulator does not model the gun's 0.6 degree Gaussian spread (~1.47 m at 140 m), so
  the real server hit rate will be somewhat lower than 59%. Best fixes are tactical
  (engage closer, prefer targets with low lateral velocity), not more lead math.
- **Mortar.** 3 shots, 0 hits, 0 damage in this recording. The only measurable shot
  (`t=22.95`, 95 m, 1.62 s flight) landed 12.7 m long / 8.4 m lateral / 19.2 m below the
  target because the target jetpacked upward at 8.9 m/s 0.15 s after the shot and kept
  changing course; at fire time the solve passed within 0.2 m of the target centre. The
  other 2 shots are untracked (the target left the recording before impact). Aim error is
  p50 0.36 deg; this is future-acceleration error, not a formula error. Flight time
  1.3-2.2 s at 95-145 m makes the mortar a low-confidence weapon against moving mechs (fine
  for stationary/slow targets and NPCs). `use_mortar` (default on) and replay `--no-mortar`
  exist for a real-match A/B; in this stream the knob is neutral (gun 27/16 either way).
- **Laser.** 7/7 aimed shots hit and the charge/release logic behaves. The residual risk is
  the 2.4 s charge+cooldown cycle and the charge dump when the target leaves vision while
  the laser is partly charged (the policy stops calling combat, the trigger defaults to
  false, and the server fires the partial charge - 0.6 s in one recorded case, 276 damage
  wasted on no target). Running at full charge is also the best DPS, so the only remaining
  lever is choosing when to start the 1.65 s cooldown.
- **Lightning.** No target ever came within 45 m in this recording, so it is only covered
  by unit tests (30 degree cone at 0.45 rad off-axis fires, 0.60 does not).
- **Replay limitations.** The hit simulation ignores enemy fire, own damage/death, loot and
  upgrades; it assumes the server's tick order is slew-then-fire (not documented); it
  evaluates shots against recorded target positions, so a target that dies in the recording
  cannot be hit. Only one hostile is visible, so per-weapon sample sizes are small.
- **Stale tracks.** `aim_for(..., now)` extrapolates a stale track by at most 1 s; the
  policy only fights threats fresher than 0.4 s, so in practice this path is unused.
- **`can_hit`** uses the base weapon ranges, not upgraded ones (`can_hit` has no `me`), a
  minor underestimate after range upgrades.

## Tuning experiments that were rejected

- **Action latency compensation** (shift self/target by the 0.05-0.075 s the action takes
  to apply): gun hits 18/31 at 0.0, 16/31 at 0.05, 17/31 at 0.075, and the standalone lead
  error grows from p50 1.90 m to 2.19/2.38 m. Not worth the complexity; removed entirely.
- **Lead scaling** (multiply the target velocity by k): hits within 2.5 m were 60% at
  k=1.0, 64% at 0.9, 61% at 0.8, 56% at 0.5 - noise-level differences, so the physically
  correct k=1.0 was kept.
- **Mortar fixed 0.25 s lead** (the old code): replaced by the exact solve; the old mortar
  aim error was p90 10.8 deg, the new one is p90 0.36 deg.
- **Laser release at 0.62 s** (the old `LASER_RELEASE`): the first 0.1 s observation at or
  above 0.62 is 0.65-0.70, worth 280-301 damage (89-96% of 315) and lower sustained DPS than
  full charge; changed to `0.72 s` threshold, which the 0.05 s charge quantum turns into an
  actual release at 0.75 s / 315 damage.

## Tests

`tests/test_combat.py` - standard library `unittest`, 26 tests, runs directly:

```
python tests/test_combat.py
```

Coverage: stationary and 19.5 m/s crossing targets at 20/80/150/220 m for gun, laser and
mortar (the solved direction is flown forward with the protocol speeds/gravity and must
pass within the 2.5 m body radius), mortar high/low ground, lightning cone and range,
stale-track extrapolation, cubes raising the target centre, fire withheld for large yaw
and pitch errors, pitch clamp, visible-now overriding grid LOS, laser hold/release/cooldown
semantics, gun reload, weapon pick order, `can_hit` boundaries and the `use_mortar` knob
(default on; off falls through to the gun and never selects the mortar).

## Conformance

From the workspace root:

```
<py> kit\check\check.py -- <py> -u bot\main.py
```

Latest run: `PASS`, ready in 55 ms with `name 'mech-royale'`, 300/300 observations
answered, 0 late, p50 0.3 ms / p90 3.1 / p99 3.3 / max 3.4 ms, 0 wrong ticks, 0 bad lines,
0 debug drawings.
