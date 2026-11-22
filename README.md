# Mech Royale — Buildathon entry

Bot for the **Mech Royale** track of the [Builders buildathon](https://www.joinbuilders.org/buildathon/tracks/builders):
16 mechs, one shrinking arena, only survival ranking counts.

The entry is this repository: the root `Dockerfile` builds the bot from `bot/`, the arena feeds one
JSON observation per tick on stdin (`protocol.md`, `mechfight.schema.json`), and the bot answers within
the 50 ms budget. Standard library only — the image must run with no network, a read-only root, one
CPU and 512 MB.

## Layout

| Path | What it is |
| --- | --- |
| `bot/` | Submission sources — protocol loop, world model, navigation, combat, policy |
| `kit/` | Official starter kit: protocol, reference clients, map, `check/check.py`, recorded stream |
| `tools/` | Offline map baking, local approximation match simulator, evaluation and reports |
| `docs/` | Design notes, tuning log, scoreboard |

## Run it locally

```sh
# protocol conformance: ready in time, one valid action per obs, 50 ms budget, clean exit
python kit/check/check.py -- python -u bot/main.py
```

```sh
# the way a match builds it (needs Docker; not available on this machine)
docker build -t mech-bot .
python kit/check/check.py --map-dir /arena -- docker run --rm -i --network none --cpus 1 \
  --memory 512m --read-only --tmpfs /tmp:rw,size=64m --user 65534:65534 \
  -v "$PWD/kit/arena:/arena:ro" mech-bot
```

## Design in one paragraph

Perception keeps a 5 s track memory of visible mechs plus a bearing-only sound model; navigation uses a
baked 2 m walkability/height grid (the map is fixed, so it is baked offline into `bot/data/nav.bin`) with
a 4 m coarse A* and 2 m local avoidance, budgeted at a few hundred expansions per tick; combat gates fire
on real aim error, leads the target, and picks the weapon by the laser > mortar > lightning > laser
triangle; policy is a small state machine (zone / evade / drop / heal / loot / fight / farm / rotate) that
trades cubes, regen and the shrinking zone against the fact that **only place matters**.

Three behaviours turned out to matter more than the rest, all measured on the real practice board:

1. **Never hide.** The 15 house bots share vision and never damage each other, so the first mech that takes
   damage is the squad's weakest and therefore its primary target for the rest of the match. Every
   defensive posture we tried (turtle, camp, heal-when-unseen) scored 0.7-2.6 against 3.6 for aggression.
2. **Fight at 210-240 m.** Their aim error is angular, so distance is armour; our lead solver still lands
   shots. 120 m scored 3.36, 190 m 3.50, 210 m 3.6, 240 m 3.45 - a sharp optimum.
3. **Weave while moving.** A lateral sinusoid (`jink`) that only switches on near a fresh enemy cuts
   incoming fire without costing much path length; heavier weave is worse because the path grows.

## Status

- [x] GitHub integration + repository bootstrap
- [x] bot v1: navigation, combat, policy, `kit/check/check.py` PASS (ready 60 ms, p99 < 4 ms)
- [x] offline simulator, evaluation harness, parallel knob screen, recorded-stream replay
- [x] 30+ real submissions; best practice score **3.59** (96 fresh seeds, builders-4)
- [x] docs: design notes, scoreboard, submission checklist
- [ ] 5.9 (top-32 cut) - needs roughly double our damage throughput, not a knob

See `docs/scoreboard.md` for every submission, what changed, and what it scored; `docs/sim.md` and
`docs/nav.md`/`docs/combat.md` for the tools and measurements.

