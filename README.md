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
triangle; policy is a small state machine (loot / rotate / camp / fight / evade / drop) that trades
cubes, regen and the shrinking zone against the fact that **only place matters**.

## Status

- [x] GitHub integration + repository bootstrap
- [ ] bot v1 (navigation + combat + policy)
- [ ] local simulation harness for A/B tuning
- [ ] first real submission and score calibration
