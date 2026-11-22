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
| 4 | `a548d11be32a78c9f578a8c6a0ba0ea10547716f` | 2026-11-21 19:39 PST | builders-3 | **3.29** (BB-39151ABF) | revert turtle; camp mode (off), finish wounded below 60%, aggro defaults (engage 260 m, 30% hp) |
| 5 | `af9930c98481c86ed6a8de9d2e8fea6530d6bb8d` | 2026-11-21 19:43 PST | builders-3 | **3.02** (BB-DF3DCAD1) | chase wounded out of sight, finish below 75%, closer standoff (gun 100/laser 150), lightning first |
| 6 | `72b2d2aec9b36d55370a8df4b83696555a92bb2a` | 2026-11-21 19:47 PST | builders-3 | **0.71** (BB-D9FA9061) | heal behind cover whenever unseen. Disaster: the bot hides and heals instead of fighting |
| 7 | `914fd366a58240a49b48340444d21915d14ada7b` | 2026-11-21 23:23 PST | builders-4 | pending (BB-3882AF7D) | squad posture v1: farm drones/crates far (300/250 m), hold ground instead of running, fight back when aimed at |

**Lessons so far**

- The local simulator is optimistic by ~1.3-1.6 against the house squad and its ordering does not
  transfer to the practice board; only real submissions are trusted.
- Passivity loses (turtle 2.35, heal-behind-cover 0.71). Aggression gained (3.29 vs 3.04).
- Real-score noise for a 48-match run is roughly +-0.3-0.6, so single small deltas mean little.

**Picking the final entry.** The site keeps the best scored commit as the final entry by default, but
once we "pick" an entry it stops auto-replacing. After each new best, pick it again on the submit page.

## What to record

- the exact commit hash that was submitted (the organisers keep a copy of every scored commit),
- the reported score and any reject reason,
- what changed in that submission and what we expected.
