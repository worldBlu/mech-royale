# Scoreboard

Practice score is `mean(16 - place)` over the practice board's matches against the house-bot squad.
Baseline to beat: **1.15** (the kit's Python example bot).

Scoring setup changed twice while we were working:

- **builders-3** (until ~2026-11-22 06:00 UTC): 48 fixed practice seeds, 4 submissions per hour.
- **builders-4** (from 2026-11-22 06:00 UTC): **96 fresh private random seeds**; identical code
  keeps its first builders-4 score, so re-submitting the same bot cannot farm luckier seeds.
  Submissions are also serialised: one submission per team is scored at a time.

Scores below are what the site reported; rows marked *builders-3* were all rescored/invalidated by
the version switch ("Scored under a previous version" in the UI), so only builders-4 rows count for
the current ranking.

| # | commit | submitted (local) | version | score | notes |
| --- | --- | --- | --- | --- | --- |
| 1 | `866bf59d4447ec75c2aca6974eaf9535970bedc4` | 2026-11-21 18:45 PST | builders-3 | **3.04** (BB-4351EDA2) | first real submission; sim said 4.3-4.7 for this build |
| 2 | `96478d51ed80c1e5df75d60ef895cbda87251430 | 2026-11-21 19:11 PST | builders-3 | **2.85** (BB-71AB9C8D) | combo policy (evade 55%/180, heal 95%/250) + `use_mortar`/terrain clamp |
| 3 | `2b5adf50b452aecb8d5f6985d1dc2d7d7da22463` | 2026-11-21 19:34 PST | builders-3 | **2.35** (BB-B883CB52) | turtle experiment: never engage, flee every contact, heal to full. Clear regression |
| 4 | `a548d11be32a78c9f578a8c6a0ba0ea10547716f` | 2026-11-21 19:39 PST | builders-3, rescored **builders-4** | **3.29** â†’ **3.27** (BB-39151ABF) | revert turtle; camp mode (off), finish wounded below 60%, aggro defaults (engage 260 m, 30% hp, rush 90 s). **Current final entry and best score**: the organisers rescored the picked builders-3 entry onto builders-4 |
| 5 | `af9930c98481c86ed6a8de9d2e8fea6530d6bb8d` | 2026-11-21 19:43 PST | builders-3 | **3.02** (BB-DF3DCAD1) | chase wounded out of sight, finish below 75%, closer standoff (gun 100/laser 150), lightning first |
| 6 | `72b2d2aec9b36d55370a8df4b83696555a92bb2a` | 2026-11-21 19:47 PST | builders-3 | **0.71** (BB-D9FA9061) | heal behind cover whenever unseen. Disaster: the bot hides and heals instead of fighting |
| 7 | `914fd366a58240a49b48340444d21915d14ada7b` | 2026-11-21 23:23 PST | builders-4 | **2.51** (BB-3882AF7D) | squad posture v1: farm drones/crates far (300/250 m), hold ground instead of running, fight back when aimed at |
| 8 | `0321067efeaac9cf10603841ade5c476d2062e36` | 2026-11-21 23:29 PST | builders-4 | **2.49** (BB-54841061) | squad v2: never run, heal only when badly hurt, farm at 400/300 m, pre-position for drops |
| 9 | `7481be935377f3f4e95f52521d88111f0e08435f` | 2026-11-21 23:51 PST | builders-4 | **2.60** (BB-D6BBD03E) | best so far: finish wounded below 90% at 35 m, disengage when outnumbered |
| 10 | `c98983141fd4c42bb95a...` (`c989831`) | 2026-11-21 23:58 PST | builders-4 | **2.51** (BB-849169FE) | brawler: rush weapons 150 s, standoff 60 m, finish below 90% |
| 11 | `2b625c7345bd3fbcbc40bdd3e9ce07eb22e4301f` | 2026-11-22 00:04 PST | builders-4 | **2.53** (BB-182E86F0) | camp: hold a low-exposure spot in the next ring, heal to full. Sim +1.25, real +0.0 |
| 12 | `8988bf09685425fd58367ab51a966fda1fd59bd9` | 2026-11-22 00:13 PST | builders-4 | **2.18** (BB-89D30D9D) | long-range laser build. Worst real score of the batch |
| 13 | `0362f4724d1e6b2bc15909b4425ef243d918a050` | 2026-11-22 00:21 PST | builders-4 | **2.74** (BB-7B3C2EA0) | camp demoted to a fallback (it was starving every loot branch), weapons looted from 150 m, heal only below 60% |
| 14 | `c23391169d3ea7d8356d1f911aa9c73f17b0c5e9` | 2026-11-22 01:27 PST | builders-4 | **3.35** (BB-6CDB28E4) | rebuilt from the 3.27 aggressive profile (`a548d11`) and only changed the finish rule (below 90%). **Best score so far; picked as the final entry** |
| 15 | `4aeed5fa8aa155be980636a94f1edba4b22f2a2c` | 2026-11-22 01:31 PST | builders-4 | **3.27** (BB-7FF714D0) | braver still: evade only under 18% hp inside 100 m, heal from 75%. No gain over 3.35 |
| 16 | `5fbb728a0f28a8f697437f48121cd8d089ba6f20` | 2026-11-22 01:36 PST | builders-4 | **3.36** (BB-49CB69F2) | 3.35 build + weapon detours from 150 m while a fight is near (only refuse inside 25 m). Current best and current final entry |

**Lessons so far**

- Every builders-4 build lands in a narrow 2.49-2.60 band while the sim spreads them over 3.0-4.9:
  the sim over-punishes aggression and over-rewards hiding, so it can rank but not calibrate.
- Passivity loses (turtle 2.35, heal-behind-cover 0.71). Aggression gained (builders-3: 3.29 vs 3.04).
- Real-score noise for a 96-seed run is roughly +-0.35, so single small deltas mean little.
- A `camp` mode that outranks the loot branches silently disables weapon pickups and cube collection;
  it now sits below them as a posture fallback.

**Picking the final entry.** The site keeps the best scored commit as the final entry by default, but
once we "pick" an entry it stops auto-replacing. After each new best, pick it again on the submit page.

## What to record

- the exact commit hash that was submitted (the organisers keep a copy of every scored commit),
- the reported score and any reject reason,
- what changed in that submission and what we expected.
| 17 | `bce2a36ae8f2edde09b11e9fd3fc49aa93e584bf` | 2026-11-22 01:40 PST | builders-4 | pending (BB-1596495A) | the aggressive profile ported onto the current policy code (loot reach, camp fallback, fight-position machinery off) |
| 18 | `fd015ddddb4e481a681571f6d6f54548f200bac8` | 2026-11-22 02:13 PST | builders-4 | pending | score fight positions: stay out of other bots' lines of sight while firing, duck while reloading |
| 19 | `6f84359d77a6855048638842ba78c2eb2081a490` | 2026-11-22 02:35 PST | builders-4 | pending | + chase a wounded target that slipped out of sight |