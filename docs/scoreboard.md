# Scoreboard

Practice score is `mean(16 - place)` over the practice board's matches against the house-bot squad.
Baseline to beat: **1.15** (the kit's Python example bot, shown on the page as 1.21-1.28 in later renders).

Scoring setup changed twice while we were working:

- **builders-3** (until ~2026-11-22 06:00 UTC): 48 fixed practice seeds, 4 submissions per hour.
- **builders-4** (from 2026-11-22 06:00 UTC): **96 fresh private random seeds**; identical code keeps
  its first builders-4 score, so re-submitting the same bot cannot farm luckier seeds. Submissions are
  also serialised: one submission per team is scored at a time.

Every builders-3 row is now tagged "Scored under a previous version" in the UI and does not count.
The organisers rescored each team's picked builders-3 entry onto builders-4 automatically.

## builders-3 (invalidated)

| # | commit | local time | score | notes |
| --- | --- | --- | --- | --- |
| 1 | `866bf59` | 11-21 18:45 | 3.04 (BB-4351EDA2) | first submission; cover by default, crash fix |
| 2 | `96478d5` | 11-21 19:11 | 2.85 (BB-71AB9C8D) | combo policy (evade 55%/180, heal 95%/250) |
| 3 | `2b5adf5` | 11-21 19:34 | 2.35 (BB-B883CB52) | turtle: never engage, flee everything - clear regression |
| 4 | `a548d11` | 11-21 19:39 | 3.29 (BB-39151ABF) | aggressive base: engage 260 m, finish 60%, weapon rush |
| 5 | `af9930c` | 11-21 19:43 | 3.02 (BB-DF3DCAD1) | chase + finish 75% + closer standoff |
| 6 | `72b2d2a` | 11-21 19:47 | 0.71 (BB-D9FA9061) | heal whenever unseen - bot hides all match |

`a548d11` was picked as the final entry at the time, and the organisers rescored it onto builders-4:
**BB-39151ABF = 3.27**. That became the anchor for the rest of the night.

## builders-4

| # | commit | local time | score | what changed |
| --- | --- | --- | --- | --- |
| 7 | `914fd36` | 11-21 23:23 | 2.51 (BB-3882AF7D) | squad posture: farm far (300/250 m), hold ground |
| 8 | `0321067` | 11-21 23:29 | 2.49 (BB-54841061) | squad v2: never run, farm at 400/300 m |
| 9 | `7481be9` | 11-21 23:51 | 2.60 (BB-D6BBD03E) | finish below 90% at 35 m, disengage when outnumbered |
| 10 | `c989831` | 11-21 23:58 | 2.51 (BB-849169FE) | brawler: rush 150 s, standoff 60 m |
| 11 | `2b625c7` | 11-22 00:04 | 2.53 (BB-182E86F0) | camp: hold a low-exposure spot, heal to full |
| 12 | `8988bf0` | 11-22 00:13 | 2.18 (BB-89D30D9D) | long-range laser build - worst of the batch |
| 13 | `0362f47` | 11-22 00:21 | 2.74 (BB-7B3C2EA0) | camp demoted to a fallback (it was starving loot), weapons from 150 m |
| 14 | `c233911` | 11-22 01:27 | 3.35 (BB-6CDB28E4) | `a548d11` + finish below 90% - reclaimed the anchor |
| 15 | `4aeed5f` | 11-22 01:31 | 3.27 (BB-7FF714D0) | braver still (evade 18%/100 m) - no gain |
| 16 | `5fbb728` | 11-22 01:36 | 3.36 (BB-49CB69F2) | + weapon detours from 150 m in combat |
| 17 | `bce2a36` | 11-22 01:40 | 2.89 (BB-1596495A) | aggressive knobs ported onto the modern code - modern code is worse |
| 18 | `82cfe5b` | 11-22 02:16 | 3.36 (BB-0A44A3AC) | fight-position scoring: shoot from outside their lines of sight |
| 19 | `175b559` | 11-22 02:26 | 3.39 (BB-2625314) | **weave while moving** (jink 0.55 @ 1.1 Hz) |
| 20 | `fdfd240` | 11-22 02:54 | 3.15 (BB-E98EB1E5) | heavier weave (0.70 @ 1.4 Hz) - worse, path cost wins |
| 21 | `30a8a36` | 11-22 03:02 | 3.45 (BB-47573691) | jink + finish below 90% |
| 22 | `eb408d6` | 11-22 03:19 | 3.50 (BB-68F92D3D) | **fight at 190-220 m** so their angular aim error works for us |
| 23 | `9d1fa5b` | 11-22 04:05 | 3.50 (BB-C2053B1D) | 205-250 m - no further gain (first attempt was rejected: commit not pushed) |
| 24 | `e3b62f0` | 11-22 04:20 | **3.52 (BB-ABC26667) - best, final entry** | farm drones/crates even with enemies within 260 m |
| 25 | `8d1e54c` | 11-22 04:40 | 3.39 (BB-33D2E007) | target by time-to-kill instead of weakest - worse |
| 26 | `4da3925` | 11-22 04:55 | 3.34 (BB-B57A9188) | boost whenever moving (19.5 vs 11.7 m/s) - worse |
| 27 | `2f979e8` | 11-22 05:00 | 3.18 (BB-9DC6A5AB) | stay in the fight: evade under 15%, heal under 60% - much worse |
| 28 | `9dcfebc` | 11-22 05:05 | 3.24 (BB-0A098A13) | weave at every range (jink distance 420 m) - worse |
| 29 | `581feef` | 11-22 05:10 | 3.30 (BB-23C3E683) | skip the first 45 s of fighting - worse |
| 30 | `4e11b23` | 11-22 05:20 | 3.51 (BB-7B508BBE) | farm drones/crates at any threat distance (400 m) - same as 260 m |
| 31 | `4e15e32` | 11-22 05:30 | 3.59 (BB-476D95C9) | fight at 210-240 m |
| 32 | `6da1d04` | 11-22 05:44 | 3.45 (BB-DD1974F3) | 220-260 m - past the optimum, too many shots miss |
| 33 | `ef1f573` | 11-22 05:58 | 3.51 (BB-DDFB6989) | heal only when the nearest seen enemy is 320 m away |
| 34 | `7b86db6` | 11-22 06:05 | 3.34 (BB-C99A9766) | finish wounded from 55 m instead of diving to 35 m |
| 35 | `ab57da8` | 11-22 06:11 | 3.48 (BB-CC802C42) | detour up to 200 m for a fallen weapon |
| 36 | `78576b4` | 11-22 06:17 | **3.83 (BB-58FCE83, confirmed by duplicate BB-9BB443F6)** | weave from 320 m out, fight at 210-240 m - **best, final entry** |
| 37 | `806392d` | 11-22 06:26 | 3.27 (BB-15D0B5A) | wider weave amplitude (0.62) at the same trigger |
| 38 | `eacb9af` | 11-22 06:31 | 3.33 (BB-B928A57F) | faster weave frequency (1.35 Hz) |

Rejected: BB-7C82F652 - `9d1fa5b` submitted before it was pushed ("commit is not in that repository").
Rejections still consume one of the hour's submissions. The site also renders stale scores for a few
seconds-to-minutes after a submission lands (we saw `BB-58FCE83` show 3.03 and 3.83 in different renders);
re-submitting identical code is free of ambiguity because it inherits the stored score ("The same code as
BB-50FCE83, so it keeps that score") - that is how 3.83 was confirmed.

Fight-distance optimum is sharp: 120 m -> 3.36, 190 m -> 3.50, 210 m -> 3.59, 220 m -> 3.45.
At 240 m+ the laser is the only weapon that reaches and its 1.65 s cooldown cannot carry a duel.

Weave (lateral sinusoid while moving, only near a fresh enemy) is the single biggest late gain:
off -> 3.59, trigger at 260 m -> 3.59, **trigger at 320 m -> 3.83**, amplitude 0.62 -> 3.27,
frequency 1.35 Hz -> 3.33, always-on (420 m) -> 3.24.

## Lessons

- The local simulator ranks builds roughly like the real practice board, but its **absolute scores are
  ~1.3-1.6 high** and it systematically over-rewards hiding and under-rewards aggression. Only the real
  board decides.
- Passivity loses every time: turtle 2.35, heal-when-unseen 0.71, camp 2.53, squad 2.51. Aggression wins:
  3.27 -> 3.35 -> 3.36 -> 3.39 -> 3.45 -> 3.50.
- Real-score noise over 96 seeds is roughly +-0.35, so deltas under ~0.3 are not evidence.
- **Hiding is structurally useless**: the 15 house bots share vision and never fight each other, so any
  damage we take makes us the weakest mech and therefore the squad's primary target for the rest of the
  match. Breaking line of sight only works for a few seconds at a time (reload/heal).
- Damage is the only real currency: 1400 HP per enemy means 8400 damage for six kills. Long-range fights
  (190-220 m) cut incoming damage because the bots' aim error is angular, while our lead solver still hits.
- `camp` mode once outranked every loot branch and silently disabled weapon and cube pickups; it now sits
  below them as a posture fallback.

## Picking the final entry

The site keeps the best scored commit as the final entry by default, but once you "pick" an entry it stops
auto-replacing. After each new best, pick it again on the submit page ("Make X our final entry").
