"""Rank exported commits with the calibrated simulator.

  python tools/rank_builds.py --commits a548d11 866bf59 96478d5 2b5adf5 72b2d2a 8988bf0

Each commit's ``bot/`` tree is exported under ``reports/exp/<sha>/bot`` and scored with
``tools/eval.py --bot-dir`` on the same seeds, in parallel.  Prints one row per build so the
ranking can be compared with the real practice-board ordering.
"""
import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def export(sha):
    out = os.path.join(ROOT, "reports", "exp", sha)
    bot = os.path.join(out, "bot")
    if os.path.isdir(bot):
        return bot
    os.makedirs(out, exist_ok=True)
    tar = os.path.join(out, "bot.tar")
    with open(tar, "wb") as fh:
        proc = subprocess.run(["git", "-C", ROOT, "archive", sha, "bot"], stdout=fh)
        if proc.returncode != 0:
            raise SystemExit(f"git archive failed for {sha}")
    subprocess.run(["tar", "-xf", tar, "-C", out], check=True)
    os.remove(tar)
    return bot


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commits", nargs="+", required=True)
    ap.add_argument("--matches", type=int, default=48)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--house-vision", default="vision", choices=("pings", "vision"))
    ap.add_argument("--knobs", default="", help="JSON knobs applied to every build")
    ap.add_argument("--python", default=sys.executable)
    args = ap.parse_args()

    out_dir = os.path.join(ROOT, "reports", "rank")
    os.makedirs(out_dir, exist_ok=True)
    running = []
    for sha in args.commits:
        bot = export(sha)
        path = os.path.join(out_dir, f"{sha}_{args.seed}.json")
        env = dict(os.environ)
        if args.knobs:
            env["MF_POLICY_KNOBS"] = args.knobs
        cmd = [args.python, "tools/eval.py", "--matches", str(args.matches),
               "--seed", str(args.seed), "--quiet", "--json-out", path,
               "--house-vision", args.house_vision, "--bot-dir", bot]
        running.append((sha, path, subprocess.Popen(cmd, cwd=ROOT, env=env,
                                                    stdout=subprocess.DEVNULL,
                                                    stderr=subprocess.DEVNULL)))
        while sum(1 for _s, _p, pr in running if pr.poll() is None) >= args.jobs:
            import time
            time.sleep(1.0)

    rows = []
    for sha, path, proc in running:
        proc.wait()
        if proc.returncode != 0 or not os.path.exists(path):
            rows.append((sha, None))
            continue
        data = json.load(open(path, "r", encoding="utf-8"))
        res = data.get("results") if isinstance(data, dict) else data
        places = [r["place"] for r in res]
        rows.append((sha, (statistics.mean(16 - p for p in places), statistics.mean(places),
                           sum(1 for p in places if p == 1), sum(1 for p in places if p <= 8),
                           sum(1 for p in places if p >= 15), len(places))))

    rows.sort(key=lambda r: -(r[1][0] if r[1] else -1))
    print(f"{'commit':9s} {'score':>7s} {'place':>6s} {'wins':>5s} {'top8':>5s} {'last2':>6s} {'n':>4s}")
    for sha, r in rows:
        if r is None:
            print(f"{sha:9s}  failed")
            continue
        score, place, wins, top8, last2, n = r
        print(f"{sha:9s} {score:7.3f} {place:6.2f} {wins:5d} {top8:5d} {last2:6d} {n:4d}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
