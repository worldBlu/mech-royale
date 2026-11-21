# Submission checklist

Track: <https://www.joinbuilders.org/buildathon/tracks/builders>
Repository: <https://github.com/worldBlu/mech-royale>
Cutoff: **2026-11-22 16:00 UTC** (= 08:00 PST, 2026-11-22). Ties break on the earlier submission.

## Hard requirements (from the track page)

| Requirement | How we satisfy it |
| --- | --- |
| Public GitHub repo at submit time | flip `worldBlu/mech-royale` to public right before submitting |
| `Dockerfile` at the repository top | `Dockerfile` in the root, `COPY bot/ /app/` |
| Builds `linux/amd64`, ≤ 10 min, network allowed **during build** | `python:3.13-slim`, no extra packages |
| ≤ 20 MB at the submitted commit | currently ~2.3 MB including the kit and map |
| Runs with no network, read-only root, uid 65534, 1 CPU, 512 MB, `/tmp` 64 MB | stdlib only, no writes outside `/tmp`, no network |
| Answers every observation within 50 ms | `kit/check/check.py` PASS; p99 well under budget |
| Passes `kit/check/check.py` | required, otherwise the submission is rejected |

## Steps at submit time

1. `git status` clean; `git log --oneline -1` records the exact commit.
2. Repo public: `PATCH /repos/worldBlu/mech-royale {"private": false}` — keep the commit hash in `docs/scoreboard.md`.
3. On the track page, paste `https://github.com/worldBlu/mech-royale` into the submit field and submit.
   - 4 submissions per hour, a submission usually scores in a few minutes.
   - Only the best practice score matters; a failed build/check is rejected and does not replace it.
4. When the score appears: record `commit / date / score / notes` in `docs/scoreboard.md`.
5. The repository may be made private again after scoring — the organisers keep the scored commit.

## After submitting

- Practice: 48 matches against 15 house bots (4 easy / 4 medium / 4 hard / 3 expert) acting as one squad —
  shared vision, never fight each other, hunt the weakest player mech first.
- Score: `mean(16 - place)`, baseline 1.15 (the kit's Python example bot).
- Top 32 qualify; final = 64-match series on new seeds. Every qualifier/finalist rebuild is from the
  submitted commit, so the repository must stay buildable.
