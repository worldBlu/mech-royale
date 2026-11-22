# Scoreboard

Practice score is `mean(16 - place)` over 48 matches against the house-bot squad. Baseline to beat: **1.15**
(the kit's Python example bot). A submission usually scores a few minutes after it is sent; 4 per hour.

| # | commit | submitted (local) | score | notes |
| --- | --- | --- | --- | --- |
| 1 | `866bf59d4447ec75c2aca6974eaf9535970bedc4` | 2026-11-21 18:45 PST | **3.04** (receipt BB-4351EDA2) | first real submission; sim said 4.3-4.7 so the sim is ~1.3-1.6 optimistic for our bot; entered in the final as best score |
| 2 | `96478d51ed80...` (`96478d5`) | 2026-11-21 19:11 PST | **2.85** (receipt BB-71AB9C8D) | combo policy (evade 55%/180, heal 95%/250) + combat `use_mortar`/terrain-clamp; sim vision said 4.69 so sim->real transfer stayed poor (delta -0.19, inside the ~+-0.3 noise of a 48-match score) |

**Picking the final entry.** The site keeps the best scored commit as the final entry by default, but once we
"pick" an entry it stops auto-replacing. After each new best, pick it again on the submit page.

## What to record

- the exact commit hash that was submitted (the organisers keep a copy of every scored commit),
- the reported score and any reject reason,
- what changed in that submission and what we expected.
| 3 | `2b5adf50b452aecb8d5f6985d1dc2d7d7da22463` | 2026-11-21 19:34 PST | **2.35** (receipt BB-B883CB52) | turtle experiment: never engage, flee every contact, heal to full. Clear regression, so passivity is wrong for this lobby |
| 4 | `a548d11be32a78c9f578a8c6a0ba0ea10547716f` | 2026-11-21 19:39 PST | **3.29** (receipt BB-39151ABF) | revert turtle; camp mode (off), finish wounded below 60%, aggro defaults (engage 260 m, 30% hp). New best; picked as final entry |
| 5 | `af9930c98481c86ed6a8de9d2e8fea6530d6bb8d` | 2026-11-21 19:43 PST | pending (receipt BB-DF3DCAD1) | chase wounded out of sight, finish below 75%, closer standoff (gun 100/laser 150), lightning first in the opening rush |
