# Bot protocol `mechfight/1`

This document is the complete specification for writing a bot. A bot is a program that pilots one mech in a 16-mech free-for-all on the Ohio State campus (Ohio Stadium to the Oval). Your bot reads observations on stdin and writes actions on stdout. The server is authoritative; your bot only sends intentions.

**Machine-readable contract:** `mechfight.schema.json` (JSON Schema, draft-07) describes every message below: `ServerMessage` (`hello`, `obs`, `end`) and `BotMessage` (`ready`, action). Generate your types from it. In the repo it is `kit/mechfight.schema.json`; in the bot kit it sits next to this file.

**Bot kit:** This document, the schema, single-file wrappers for Python (`python/mechfight.py`, standard library only) and TypeScript (`ts/mechfight.ts`, no dependencies) with an example bot each, Dockerfiles, a conformance check, and the map. The wrappers only handle the stdin/stdout loop, typed messages, and debug drawing. Everything else (navigation, aiming, and a model of the world) is yours to build.

## 1. Packaging, testing, and submitting

### Docker packaging

Your bot is a Docker image whose entrypoint speaks this protocol on stdin and stdout. The kit's `python/Dockerfile` and `ts/Dockerfile` are ready to use:

```dockerfile
FROM python:3.13-slim
WORKDIR /bot
COPY . .
ENTRYPOINT ["python3", "-u", "/bot/example_bot.py"]
```

- There is no network at runtime, so vendor every dependency into the image.
- Use `-u` (or flush stdout after every line) so output is not buffered.
- Print logs to stderr. Anything on stdout that is not a protocol message fails the conformance check.
- The bot runs as an unprivileged user (uid 65534, not root) on a read-only disk. Install packages system-wide (not `pip install --user` or into `/root`) and write files only to `/tmp`.

### Conformance check

`check/check.py` (standard library only) feeds your bot a recorded `hello` and 300 observations from a real match. It checks that your bot:

- sends `ready` within the hello budget
- sends one schema-valid action per observation, echoing its `tick`
- answers within `tick_budget_ms` (more than 2% late fails; a late pass is retried with a fresh bot, up to 3 passes, and the best counts)
- answers only the newest observation when several are queued
- keeps debug drawings inside the caps
- exits cleanly after `end`

`check/check.py` is not a simulation. The observations do not react to your actions.

```sh
python3 check/check.py -- python3 -u python/example_bot.py
docker build -t my-bot python
python3 check/check.py --map-dir /arena -- docker run --rm -i --network none --cpus 1 --memory 512m --read-only --tmpfs /tmp:rw,size=64m --user 65534:65534 -v "$PWD/arena:/arena:ro" my-bot
```

`--map-dir /arena` makes `hello.map.dir` the path the map has inside the container, as in a match. Without it the check sends the kit folder's path on your machine, which a container cannot open. The `docker run` flags are the match sandbox (§3): the scorer runs this same check that way before your matches, so a bot that passes only without them is rejected.

### Submitting for real games

Push your bot to a public GitHub repository with the `Dockerfile` at its top level (copy the files of `python/` or `ts/` there), and submit it on the Mech Royale track page (joinbuilders.org/buildathon). The repository may hold 20 MB at the commit you submit. It is built on linux/amd64 with the network on, and the build must finish in 10 minutes. Each submission is built, checked with `check.py`, and played in 48 practice matches against built-in bots. The page shows your score, or what to fix when a submission is rejected.

## 2. Match lifecycle and timing

```
server → bot   {"type":"hello", ...}           once
bot → server   {"type":"ready", ...}           within hello_budget_ms (20 s; covers container start)
loop, 10 times per match-second while your mech is alive:
  server → bot {"type":"obs", "tick":N, ...}
  bot → server {"tick":N, ...action...}         within tick_budget_ms (50 ms)
server → bot   {"type":"end","place":P,"kills":K}   then stdin closes; exit
```

The match is **lockstep**. The server sends all 16 observations at once, waits until every bot answers or the 50 ms budget runs out, applies the actions, and simulates 2 ticks (0.1 s of match time). Scored matches run much faster than real time. A fast bot speeds up the match and is never at a disadvantage for answering early.

After your mech dies, you receive no more observations, only `end`.

### Timing rules

- **50 ms per decision:** You have 50 ms of wall time per decision, measured by the server from sending `obs` to reading your line. Your CPU is capped to 1 core.
- **Echo the tick:** Every action must carry `"tick"` equal to the observation's `tick`. Answers with any other tick (including a late answer to an earlier observation) are dropped.
- **No answer = coast, then idle:** If no valid answer arrives in time, your mech keeps the *continuous* parts of your last action (`move`, `boost`, `aim`) and drops the one-shot parts (`fire`, `jump`, `reload`, `pick`, `take`, `drop`). After `coast_s` (1.0 s) of missed answers, your mech idles: no movement, aim held. Idling starts on the first decision tick past `coast_s`, so up to one decision (0.1 s) later.
- **Crash = idle:** If your process exits, your mech idles for the rest of the match. The match goes on.
- **Fall behind gracefully:** If observations queue up on stdin, skip to the newest one and answer that. The kit wrappers do this for you.

## 3. Transport and runtime limits

- **JSON lines:** Send one JSON object per line, UTF-8, `\n`-terminated, at most 64 KiB per line (`bot.max_line_bytes`, measured on the raw line: trailing spaces count).
- **stdin and stdout:** **stdin** carries server→bot messages. **stdout** carries bot→server messages. Flush stdout after every line.
- **stderr:** Reserved for your logs. Matches do not show stderr to you. When your bot fails the conformance check at submission, the end of its stderr appears in the message.
- **Bad lines and bot name:** Lines that are not valid JSON, or that exceed the line limit, are counted as bad lines and ignored. A bot's `ready` name is cut to 32 characters with control characters removed.
- **Container sandbox:** Your process runs in Docker as an unprivileged user (uid 65534, no capabilities) with:
  - **no network**
  - a read-only filesystem (writable `/tmp`, 64 MB)
  - 1 CPU and 512 MB RAM (no swap)
  - at most 128 processes and threads
  - map files mounted read-only at `/arena`

## 4. Scoring

- Placement is the order mechs die in: the last mech alive is 1st, and the first to die is 16th.
- Mechs that die on the same tick share a placement.
- If the clock runs out, survivors are ranked by HP.
- Each match gives `16 - place` points (15 for a win, 0 for dying first).
- Your score is the mean over the matches you play. Surviving matters more than kills, though kills give you cubes.

## 5. Coordinates and units

- **Units:** Meters, seconds, radians.
- **Axes:** **x = east, y = north, z = up.** Heights are relative to the ground at the map origin (lat 40.0005, lon −83.0165), the same frame as `v2/world.json`.
- **Yaw and pitch:** **Yaw** is measured from +x (east) counter-clockwise: 0 = east, π/2 = north, ±π = west. **Pitch** is up-positive. A direction vector is `[cos(pitch)·cos(yaw), cos(pitch)·sin(yaw), sin(pitch)]`.
- **Mech dimensions:** Mech `pos` is the **feet** (bottom center). A mech is a vertical cylinder of radius 2.5 m and height 9 m:
  - center of mass: `pos.z + 4.5`
  - eyes: `pos.z + 8`
  - gun muzzle: `pos.z + 7`
  - mortar tube: `pos.z + 9.5` (`height + 0.5`)
- **Shot origin:** Every shot leaves `radius + 0.5` = 3.0 m out along the aim (less if a wall is closer, so nothing fires from inside geometry).
- **NPC dimensions:** Radius 1.8, height 3.5.

## 6. Messages

### `hello` (server → bot)

```json
{
  "type": "hello",
  "protocol": "mechfight/1",
  "slot": 3,
  "seed": 2873465123,
  "map": { "dir": "/arena", "files": ["v2/world.json", "v2/terrain.i16"] },
  "config": { "match": {...}, "bot": {...}, "mech": {...}, "vision": {...}, "sound": {...},
              "weapons": {...}, "projectile": {...}, "cubes": {...}, "crates": {...}, "loot": {...}, "npc": {...}, "drops": {...},
              "zone": {...}, "replay": {...}, "debug": {...}, "upgrades": [...] }
}
```

- `slot` is your mech id for this match (0–15).
- `seed` is yours: seed your RNG with it to make your bot reproducible. It is fixed per slot and says nothing about the match (it is not the match seed).
- `config` is the **complete rule set** the server uses. Read numbers from it instead of hard-coding them, because balance changes between matches.

### `ready` (bot → server)

```json
{ "type": "ready", "name": "my-bot" }
```

- `name` (≤ 32 chars) is shown on the spectator stream.

### `obs` (server → bot)

A real observation (trimmed only where marked):

```json
{
  "type": "obs", "tick": 508, "t": 25.4, "alive": 15,
  "self": {
    "id": 1, "pos": [-217.98, -22.04, -4.73], "vel": [6.22, -6.5, 0], "aim": [0.935, -0.143],
    "hp": 580, "max_hp": 1400, "energy": 19, "max_energy": 100, "grounded": true,
    "cubes": 0, "upgrades": [], "in_zone": true,
    "weapons": [
      { "kind": "gun", "cooldown": 0.03, "ammo": 10, "reload": 0,
        "stats": { "damage": 45, "cooldown": 0.24, "speed": 252, "range": 230, "spread_deg": 0.6, "mag": 12, "reload": 2.4 } },
      null
    ],
    "stats": { "walk_speed": 11.7, "boost_speed": 19.5, "turn_rate": 4, "muzzle_height": 7, "...": "your [mech] numbers after upgrades" }
  },
  "mechs": [
    { "id": 5, "kind": "mech", "pos": [-208.24, -7.7, -4.7], "vel": [7.42, -5.1, 0], "aim": [-2.173, -0.147],
      "hp": 950, "max_hp": 1400, "cubes": 0, "weapons": ["gun", "lightning"], "charging": false },
    { "id": 104, "kind": "npc", "pos": [10.2, 55.1, 0.3], "vel": [1.1, 0, 0], "aim": [0.1, 0], "hp": 350, "max_hp": 350 }
  ],
  "projectiles": [ { "id": 280, "kind": "gun", "pos": [-211.61, -10.94, -0.45], "vel": [192.67, 161.87, -13.28] } ],
  "crates": [ { "id": 1007, "pos": [12.5, -40.2, 0.1], "hp": 300 } ],
  "cubes": [ { "id": 5012, "pos": [14.0, -41.0, 0.1] } ],
  "pickups": [ { "id": 8013, "kind": "mortar", "pos": [-214.6, -19.3, -4.71] } ],
  "sounds": [
    { "kind": "lightning", "bearing": 1.167, "dist": "near", "age": 0.1 },
    { "kind": "gun", "bearing": -2.68, "dist": "near", "age": 0.05 }
  ],
  "beams": [ { "by": 5, "kind": "lightning", "path": [[22.1, -40.3, 6.2], [14.0, -44.1, 4.5]], "age": 0.1 } ],
  "events": [ { "type": "damaged", "amount": 90, "weapon": "lightning", "bearing": 0.974, "by": 5, "rear": true } ],
  "drops": [ { "id": 20000, "pos": [-80.4, 12.9, -3.1], "radius": 8, "lands_in": 11.3, "capture_s": 4, "progress": 0, "holder": null } ],
  "zone": { "center": [41.3, -72.8], "radius": 560, "next_center": [-23.72, 2.21], "next_radius": 380,
            "shrink_start": 60, "shrink_end": 110, "dps": 20 },
  "offer": null
}
```

| Field | Meaning |
| --- | --- |
| `tick`, `t` | Sim tick (20 per second) and match time in seconds. Echo `tick`. |
| `alive` | Mechs still alive (NPCs not counted). Your placement if you died now would be `alive`. |
| `self.aim` | Your torso's **actual** aim. It slews toward the aim you command at `turn_rate` (yaw) and `pitch_rate` rad/s. Shots leave along this, not along what you asked for. |
| `self.grounded` | On a surface. Jumping requires it. |
| `self.weapons[i]` | Slot i, or `null` if the slot is empty (slot 1 is empty at spawn). `cooldown`: seconds until it can fire. Gun: `ammo`, `reload` (seconds left, 0 = not reloading). Laser: `charge` (seconds charged so far; 0 = not charging). `stats`: this weapon's numbers after your upgrades. |
| `self.stats` | Your `[mech]` numbers after upgrades (speeds, turn rate, armor, bonuses). |
| `mechs` | Everything you can **see** right now: other mechs (`kind: "mech"`) and NPC drones (`kind: "npc"`). `aim` is their torso aim; `charging` = their laser is charging (it fires along their aim when they let go). `weapons` lists what they hold (empty slots omitted). `hp` is shown while `vision.show_enemy_hp` is true. |
| `projectiles` | Visible enemy gun bullets, mortar shells, and NPC bullets with position and velocity. Shells fall at `weapons.mortar.gravity`. Your own are omitted. |
| `crates`, `cubes` | Visible loot crates (with HP) and power cubes. |
| `pickups` | Visible weapons lying on the ground: `id`, `kind`, `pos`. Same visibility rules as cubes. Take one with the `take` action. |
| `sounds` | Everything you **heard** since your last observation. See §8. |
| `beams` | Laser beams and lightning arcs fired by other mechs since your last observation that you could see (either end, or the middle of a segment, in view): `{ by, kind: "laser" \| "lightning", path: [[x,y,z], ...], age }`. `path` starts at the shooter's muzzle; a laser has two points, a lightning arc one per jump. |
| `events` | Things that happened to you since your last observation. See below. |
| `drops` | Supply drops that have been called and not yet taken (always known, like the zone). `lands_in`: seconds until it is on the ground (0 = capturable). `holder`: the mech whose capture clock is running, `progress`: seconds it has banked of `capture_s`. See §7, *Supply drops*. |
| `zone` | Always known. See §9. |
| `offer` | A pending upgrade choice, or null. See §10. |

Events:

| `type` | Fields | Meaning |
| --- | --- | --- |
| `damaged` | `amount`, `weapon`, `bearing`, `by`, `rear?` | You took damage. `bearing` is the world-frame yaw (same convention as `aim[0]`, not relative to your facing) toward where it came from (null for zone). `by` is the attacker's id (null for zone). `rear: true` when it came from outside your front arc (rear armor, §7). |
| `hit` | `target`, `amount`, `weapon`, `effect?`, `rear?` | You damaged `target` (mech, NPC, or crate id; crate hits are reported since 2026-11-16). `effect` is set when the hit carried a signature effect (`cookoff`, `short`, `concuss`); `rear: true` when you hit a mech's back. |
| `effect` | `effect`, `by` | A signature effect landed on you: `cookoff` (a laser set off the mortar you carry), `short` (lightning took your laser offline), `concuss` (a mortar blast threw you and drained energy). `by` is who did it. |
| `drop` | `id`, `cubes` | You captured supply drop `id`; `cubes` is your new total. |
| `kill` | `target` | You killed a mech. |
| `pickup` | `cubes` | You picked up a cube; `cubes` is your new total. |
| `upgrade` | `id` | An upgrade was applied (your pick or the timeout default). |
| `took` | `id`, `kind`, `slot` | Your `take` succeeded: pickup `id` is now in `slot`. |
| `dropped` | `id`, `kind`, `slot` | A weapon left your `slot` and lies on the ground as pickup `id` (a swap or a `drop`). |
| `take_failed` | `id`, `reason` | Your `take` didn't happen. `reason`: `gone` (no such pickup, or someone closer took it this tick), `range`, `have` (you already hold that kind), `full` (no empty slot and no `drop`), `busy` (the slot's laser is charging). |

`weapon` is one of `gun`, `mortar`, `laser`, `lightning`, `npc`, `zone`.

Id ranges: mechs 0–15, NPCs 100+, crates 1000+, cubes 5000+, weapon pickups 8000+, supply drops 20000+.

### action (bot → server)

```json
{ "tick": 508, "move": [0.7, -0.7], "boost": false, "jump": false,
  "aim": [0.95, -0.05], "fire": [true, false], "reload": false, "pick": 1, "take": 8013 }
```

| Field | Default | Meaning |
| --- | --- | --- |
| `tick` | required | Echo of the observation tick. |
| `move` | `[0,0]` | Desired horizontal direction in the world frame. Length is clamped to 1; length < 1 walks slower. Legs are independent of the torso: you can move one way and aim another. |
| `boost` | false | On the ground while moving: run at `boost_speed`, draining `boost_drain`/s energy. In the air: jet upward (`jet_accel`, capped at `max_rise_speed`), draining `jet_drain`/s. |
| `jump` | false | Jump if grounded (`jump_speed` upward). |
| `aim` | last aim | Desired `[yaw, pitch]`. The torso slews toward it; pitch is clamped to `[min_pitch, max_pitch]`. |
| `fire` | `[false,false]` | Hold the trigger of slot 0 / slot 1 until the next decision (2 ticks). |
| `reload` | false | Start a gun reload early. |
| `pick` | none | Index (0–2) into the current offer's `choices`. |
| `take` | none | Id of a weapon pickup within `loot.pickup_radius` (4 m horizontally) and 4 m vertically (fixed, not configurable). It goes into your empty slot, or into slot `drop` if given. |
| `drop` | none | Slot 0 or 1. With `take`: the slot the new weapon replaces; the old one is left on the ground at your feet. Alone: empty that slot, leaving the weapon on the ground. |
| `debug` | none | Debug drawing for your team (below). Never affects the match. |

Each action applies for the 2 ticks until the next decision. Missing fields take the defaults above (`aim` defaults to your previous commanded aim). Unknown fields are ignored. A dead mech's place arrives in `end`.

### Debug drawing

`debug` attaches drawings to an action for your own tools. The server checks them against the caps below, keeps them out of the public replay, and never reads them to play the match. Nothing on the track shows them to you.

```json
{ "tick": 508, "move": [1, 0],
  "debug": {
    "label": "chasing 5",
    "lines":  [ { "a": [-217.9, -22.0, 2.3], "b": [-208.2, -7.7, -0.2], "color": "#ff5040" } ],
    "points": [ { "at": [-23.7, 2.2, -4.7], "color": "#40c0ff", "size": 6 } ],
    "text":   [ { "text": "drop here", "at": [-80.4, 12.9, 0], "color": "#ffd060" }, { "text": "low energy" } ] } }
```

| Field | Meaning |
| --- | --- |
| `lines[]` | `a`, `b`: world points (x east, y north, z up, meters, the frame of §5). |
| `points[]` | `at`: a world point. `size`: radius in screen pixels, 1–12 (default 4). |
| `text[]` | `text`, and `at` (a world point) or no `at` to stack it above your mech. |
| `label` | One short line above your mech. |
| `color` | `"#rgb"` or `"#rrggbb"` on any item. Missing or invalid: your mech's color. |

Retention and caps:

- A drawing stays on screen until your next action that carries `debug`, or for `debug.hold_s` (0.5 s), whichever comes first. Send `"debug": {}` to clear it early.
- The server cuts each drawing to the caps in `hello.config.debug` and says nothing:
  - `max_lines` (64) lines
  - `max_points` (64) points
  - `max_text` (16) texts
  - `max_text_chars` (80) characters per text and label
  - `max_bytes` (8192) bytes serialized, keeping the label, then text, lines, and points in that order
- Bad items are dropped and numbers are rounded to 0.01.
- Once a mech has recorded `max_match_bytes` (8 MB) in a match, later drawings are dropped.
- The whole action line still has to fit in `bot.max_line_bytes`.

## 7. Rules of the world

All numbers below are defaults. Read the live values from `hello.config`.

### Movement

Mechs accelerate toward the commanded velocity (`ground_accel` 58.5 m/s² on the ground, `air_accel` 12 in the air), so changing direction takes time. You are a vertical cylinder standing on the surface under your center.

- **Walls** stop the part of your move that goes into them and keep the part that goes along them: you slide. You never end up inside a wall.
- **Ledges** up to `step_up` (1.2 m) are walked up. Anything taller is a wall unless your feet are already above it: jump and jet (boost in the air) until they clear the edge, then move over it.
- **Slopes** steeper than `max_slope_deg` (50°) cannot be walked up, at any speed. Go around or jet.
- **Bridges and overhangs** that clear your head (9 m) can be walked under, and their underside stops a jet. Lower overhangs are walls, and so is a step up under one that would put your head into it. An eave over any part of you stops a jet too; unless a wall holds you under it, you are slid out from under it (up to 0.6 m per tick) and climb on.
- **Roof edges:** Coming down past the edge of a roof you still overhang (your center is off it), you land on the edge instead of dropping into the wall below. Keep moving toward the roof and you step onto it; otherwise you are slid off (up to 0.6 m per tick) and fall clear of the wall. If a wall on the other side holds you there, you stay on the edge: nothing falls into a gap narrower than itself.

With 100 energy you can climb about 60 m (almost every campus roof; the 70 m towers are out of reach). Energy regenerates at `energy_regen`/s after `energy_regen_delay` s without spending. Gravity is 22 m/s². The map edge is a wall. Overlapping mechs are pushed apart.

### Health

You start with 1400 HP. After `regen_delay` (5 s) without dealing or taking damage, you regenerate `regen_per_s` (45) HP/s, except while outside the zone. Zone damage counts as taking damage: the 5 s timer starts again when you step back inside. Armor (upgrades) reduces weapon damage, not zone damage.

### Rear armor

A hit from outside your torso's front arc (`mech.front_arc_deg` 180°, centered on your aim yaw) deals `mech.rear_mult` (×1.5) damage. The direction is where the shot came at you from: the bullet's path, the beam's or arc's origin, or the blast point of a shell. Facing your attacker protects you; `damaged.rear` tells you when you were hit from behind.

### Weapons

Every shot leaves along your *actual* torso aim. There is no lock-on. You start with only the `gun` (slot 0); the other weapons are loot (below). Firing an empty slot does nothing.

| Weapon | How it works |
| --- | --- |
| `gun` | Projectile at `speed` 252 m/s, `damage` 45, one shot per `cooldown` 0.24 s (held down, exactly that rate), `mag` 12 then `reload` 2.4 s, small Gaussian spread (`spread_deg`). Flies straight (no drop) for `range` m. You must lead moving targets. |
| `mortar` | Shell launched at `speed` 72 m/s along your aim, falling at `gravity` 22 m/s² (so pitch sets range; max range ≈ v²/g ≈ 235 m at 45°). Explodes on impact: `damage` 200 to a directly hit target, plus `splash` 160 falling to 30% at `splash_radius` 7 m, blocked by walls. Lob it over buildings at targets you can't see. Your own splash hurts you at 50%. **Concussion:** every mech the blast reaches is thrown away from it (`knock` 16 m/s, `knock_up` 9 m/s) and loses `drain` 45 energy, scaled by the same falloff. Your own blast throws you too. |
| `laser` | **Hold the trigger to charge, let go to fire.** The charge builds for up to `charge` 0.75 s; let go and the hitscan beam fires along your aim at that moment (`range` 280 m). Damage scales with the charge, from `min_frac` 35% at none to `damage` 315 at full; let go before `min_charge` 0.15 s and it fires as soon as that much is in, and a full charge held for `hold_s` 2 s more fires on its own. Then `cooldown` 1.65 s. While charging you move at half speed; your torso turns as usual (`charge_turn_mult` 1), so the beam goes where you aim when you let go. Everyone who can see you sees `charging: true` and your `aim`, and it makes a sound, so the line can be dodged. Your `charge` (in `self.weapons`) is the seconds charged so far. **Cook-off:** +60% (`cookoff`) to a mech that is holding a mortar. |
| `lightning` | Instant. Hits the nearest target (mech, NPC or crate) within `range` 45 m and within `cone_deg` 30° of your aim yaw, with line of sight, for `damage` 60, then chains to up to `chain` 2 more targets within `chain_range` 14 m (×0.7 damage per jump). Costs `energy` 3 per shot; fires every `cooldown` 0.25 s. Without a target it still fires (and costs energy): the arc goes out along your aim to the first wall or `range`. **Short:** a mech it hits that holds a laser loses any charge in progress and can't start one for `short_s` 2 s. |

**Weapon triangle:**

- Laser beats mortar (cook-off, plus it outranges the shell).
- Mortar beats lightning (concussion keeps it out of arc range and drains the energy it fires with).
- Lightning beats laser (short).
- The gun is neutral.

What you carry against what opponents carry is a decision, and so is which weapon you pick up.

**Cover and shots:** Gun bullets, mortar shells, and lasers hit buildings, bridges, and terrain exactly where you see them. You can shoot under a bridge. A shot whose barrel pokes into a wall stops at that wall. **Trees do not stop shots:** you can fire blind into trees at a sound.

**Cube scaling:** Damage is scaled by power cubes (§10): `× (1 + damage_per_cube × min(cubes, stat_cap))`.

### NPC drones

NPC drones (`kind: "npc"`, 12 per match, 350 HP) wander within `npc.leash` (30 m) of home.

- They shoot (25 damage, 90 m/s bullets, 1 s cooldown) at whoever attacks them, or at mechs within 45 m with line of sight while the drone is within 30 m of home.
- A drone chases until its target is 90 m away or the drone is `npc.chase_leash` (60 m) from home, then walks back.
- Each drone drops 2 cubes. Drones do not take zone damage.
- **Cubes** go to the closest mech within `cubes.pickup_radius` (ties go to the lower id).

### Crates

There are 36 crates per match (300 HP, about a quarter on rooftops). Destroying a crate drops 1 cube, and drops a weapon with probability `loot.crate_weapon_chance` (0.5).

### Weapon loot

Weapons lie on the ground as pickups (`obs.pickups`, ids 8000+). They come from:

- `loot.floor_count` (40) spawns at match start on open ground and roofs (a quarter on roofs)
- destroyed crates (above)
- dead mechs, which drop every weapon they held except the gun

Loot rules:

- Random spawns use `loot.weights` (mortar, laser, lightning 1:1:1; gun 0).
- Taking a weapon is explicit: send `"take": <id>` while within `loot.pickup_radius` (4 m).
- It goes into your empty slot. If both slots are full, also send `"drop": <slot>` to swap it in, leaving the old weapon on the ground at your feet (anyone can pick it up).
- You cannot hold two weapons of the same kind.
- A weapon you take cannot fire for `max(loot.equip_s, its cooldown)` seconds (1 s for the gun, 1.65 s for a laser). A dropped gun keeps its ammo, so swapping never skips a cooldown or a reload.
- You cannot drop or swap out a laser while it is charging.
- You can also drop the gun.
- If two mechs take the same pickup on the same tick, the closer one gets it.
- Weapons on the ground never despawn, though the zone closes over them.

### Supply drops

There are three supply drops per match (`drops.times`: called at 70, 140, and 210 s).

- Each drop lands `fall_s` (25 s) after it is called, on open ground inside the circle the zone is closing to.
- Every mech sees it in `obs.drops` from the moment it is called.
- Once it is on the ground, stand inside its ring (`radius` 8 m) **alone** for `capture_s` (4 s) to gain `cubes` (3) power cubes, full HP, and full energy.
- While two or more mechs are in the ring, the capture clock freezes. If the ring empties, or a different mech becomes the only one in it, the clock restarts.

## 8. Vision and sound

### Vision

**You see** an object if your eye (`pos.z + 8`) has line of sight to its center or top, and either:

- it is within `near_range` (45 m) in any direction (proximity sensors; buildings block, canopy does not), or
- it is within `range` (200 m) and inside your `fov_deg` (120°) **yaw wedge** centered on your torso aim yaw (horizontal only: pitch does not matter, so you see straight up and down), with no building blocking the line.

Canopy is soft: a line through a little foliage still sees, while a line through a dense crown does not (`rules.canopyDensity` / `canopyBlockT` in `v2/world.json`). Tree trunks block sight but not shots.

Once you have seen a mech or an NPC drone, you keep tracking its real, current position for `memory_s` (5 s) after it leaves view, so a mech ducking behind cover does not vanish immediately. Everything else (crates, cubes, weapon pickups, projectiles) is listed only while you see it.

Where you aim is where you look. Turning your torso to scan costs aim time.

### Sound

**You hear** sounds within their carry range (`sound.range`):

- `gun`: 320 m
- `mortar` launch: 420
- `explosion`: 420
- `laser`: 380
- `laser_charge`: 140
- `lightning`: 160
- `jet`: 90
- `death`: 600
- `crate`: 220
- `npc_gun`: 200
- `step`: 40
- `boost`: 90
- `reload`: 60
- `drop`: 600 (a supply drop landing)

A mech moving faster than `sound.step_speed` (5 m/s) on the ground makes a `step` every `sound.step_every` (0.6 s), or a `boost` while boosting. Walking slowly or standing still is silent. Each weapon has its own kind, so a sound tells you what the shooter is holding.

Each heard sound gives:

- `bearing`: world-frame yaw to the source (like `aim[0]`, not relative to your facing), with Gaussian error (σ = `bearing_noise_deg` 7°)
- `dist`: `"near"` (< 60 m), `"mid"` (< 160 m), or `"far"`
- `age`: seconds since it happened

Your own sounds are not reported. Sounds do not say who made them.

## 9. The zone

The zone is a circle that shrinks in phases. Outside it, you lose `dps` HP per second (no armor, no regen).

`zone` always gives the current circle, the **next** circle it will shrink to, and the shrink window:

- Before `shrink_start`, the circle holds still.
- Between `shrink_start` and `shrink_end`, the center and radius move linearly from the current circle to the next one.
- The next circle is always entirely inside the current one.

Defaults:

- Starts at radius 560 m around (41.3, −72.8), then shrinks 380 → 240 → 140 → 70 → 25 → 0 m.
- Each phase's damage applies from its hold through its shrink: 20, 35, 60, 100, 160, and 260 HP/s (`zone.dps` gives the current value).
- The last circle closes by about 6:55. Matches end when one mech is left or at 8:00, whichever comes first.

## 10. Cubes and upgrades

- Picking up a cube (walk within `pickup_radius` 3.5 m horizontally and 4 m vertically) gives +4% max HP (healing the difference) and +4% damage, up to `stat_cap` 10 cubes.
- Killed mechs drop all their cubes plus two.
- Each cube also makes you `size_per_cube` 5% bigger, with no cap: radius, height, eye, and muzzle height all scale by 1 + 0.05 × cubes (`self.stats` shows your current `eye_height` and `muzzle_height`). A bigger mech is easier to hit, and sees and shoots from higher up.

Every time your cube count reaches a threshold in `cubes.upgrade_thresholds` (1, 3, 6, 10, 15, 21, …), you receive an **offer**:

```json
"offer": { "id": 2, "expires": 61.35, "choices": [
  { "id": "gun_rate", "name": "Rotary feed", "desc": "+15% arm gun fire rate" },
  { "id": "armor", "name": "Reactive armor", "desc": "-8% damage taken" },
  { "id": "sensors", "name": "Sensor suite", "desc": "+15 m proximity sensors, +25 m vision, halves sound bearing error" } ] }
```

- Answer with `"pick": i`. If you do not pick by `expires` (5 s), choice 0 is applied.
- Offers queue; the next offer appears after you resolve the current one.
- Choices are 3 distinct upgrades you can still take (weapon upgrades only for weapons you hold when the offer is drawn, each up to its `max` stacks).
- Weapon upgrades belong to your mech, not the item: they persist if you drop that weapon and apply again if you pick up the same kind later.
- The full list and exact effects are in `hello.config.upgrades`: each effect multiplies (`mul`) or adds to (`add`) one of your `self.stats` or `self.weapons[i].stats` numbers.

## 11. Map files

`hello.map.dir` (`/arena` in Docker) holds the static map. The map is the same every match; spawns, crates, NPCs, loot, the zone, and supply drops are random per match.

Two files define the world the server simulates:

- `v2/world.json`: `bounds`, `terrain` (grid description), `prisms`, and `trees`.
  - `prisms[]`: every solid that is not terrain. Each prism has `kind` (`building`, `stadium`, `bridge`), `base` and `top` heights, `ring` (footprint polygon, flat `[x0, y0, x1, y1, ...]`), and `holes` (same format). A point is inside a prism if it is inside the ring, outside the holes, and between `base` and `top`. A bridge has a `base` above the ground: you walk and shoot under it.
  - `trees`: `fields` names the columns of the flat `data` array (`x`, `y`, `z`, `height`, `crownR`, `crownBase`, `trunkR`, ...). Trees block vision only (crowns fade it, trunks stop it).
  - `rules`: `maxStep`, `maxSlopeDeg` (the viewer's walking rules; the server uses `config.mech`) and the canopy numbers in §8.
  - Scenery the server ignores: `blocks` (which prisms make up each named building), `stadium` (the stadium's shape for drawing; its solid parts are the `stadium` prisms), `cars`, `parking`, `water`, `barriers` (fences, walls, hedges), and `stats`. None of them block movement, sight, or shots, and water does not slow you.
- `v2/terrain.i16`: little-endian int16 centimeters, `terrain.nx × terrain.ny` (1070 × 724), row-major from (`terrain.minX`, `terrain.minY`) with `terrain.step` = 1 m. Index `j * nx + i` is the ground height × 100 at `x = minX + i·step`, `y = minY + j·step`. Sample it bilinearly.

## 12. Versioning

`hello.protocol` is `"mechfight/1"`. Fields may be **added** within version 1, never removed or changed in meaning. Ignore fields you do not know. A breaking change bumps the version.
