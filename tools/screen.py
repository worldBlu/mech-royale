"""Parallel policy screen: run several knob sets on the same seeds, compare paired scores.

  python tools/screen.py --matches 192 --jobs 6 --out reports/screen1

Each config runs ``tools/eval.py --quiet --json-out`` in its own process with
``MF_POLICY_KNOBS`` set, so one config cannot leak state into another.  The first config
is the baseline; every other config is reported as a paired delta (same seeds).

Rows:  config  matches  mean_score  delta_vs_base  sd(delta)  wins  top8  first_deaths
"""
import argparse
import json
import os
import statistics
import subprocess
import sys
import time

CONFIGS = [
    ("base", {}),
    ("s30p09", {"strafe_step": 30.0, "strafe_period": 0.9, "strafe_jitter": 0.4}),
    ("s40p09", {"strafe_step": 40.0, "strafe_period": 0.9, "strafe_jitter": 0.4}),
    ("s50p09", {"strafe_step": 50.0, "strafe_period": 0.9, "strafe_jitter": 0.4}),
    ("s40p07", {"strafe_step": 40.0, "strafe_period": 0.7, "strafe_jitter": 0.5}),
    ("s40p12", {"strafe_step": 40.0, "strafe_period": 1.2, "strafe_jitter": 0.4}),
]


def run(cfg, knobs, matches, seed, out_path, py):
    env = dict(os.environ)
    env["MF_POLICY_KNOBS"] = json.dumps(knobs)
    cmd = [
        py, "tools/eval.py",
        "--matches", str(matches),
        "--seed", str(seed),
        "--quiet",
        "--json-out", out_path,
    ]
    t0 = time.time()
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.stderr.write(f"[{cfg}] eval failed: {proc.stderr[-2000:]}\n")
    return cfg, out_path, time.time() - t0, proc.returncode


def load(path):
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, dict):
        return data.get("results") or data.get("matches") or []
    return data


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--matches", type=int, default=192)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--out", default="reports/screen1")
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--only", default="")
    ap.add_argument("--house-vision", default="pings", choices=("pings", "vision"))
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    configs = CONFIGS
    if args.only:
        wanted = {n.strip() for n in args.only.split(",") if n.strip()}
        configs = [c for c in configs if c[0] in wanted]
    base_name = configs[0][0]

    pending = list(configs)
    running = []
    results = {}
    while pending or running:
        while pending and len(running) < args.jobs:
            cfg, knobs = pending.pop(0)
            path = os.path.join(args.out, f"{cfg}_{args.seed}.json")
            env = dict(os.environ)
            env["MF_POLICY_KNOBS"] = json.dumps(knobs)
            cmd = [args.python, "tools/eval.py", "--matches", str(args.matches),
                   "--seed", str(args.seed), "--quiet", "--json-out", path,
                   "--house-vision", args.house_vision]
            running.append((cfg, path, subprocess.Popen(cmd, env=env,
                                                        stdout=subprocess.DEVNULL,
                                                        stderr=subprocess.DEVNULL)))
        time.sleep(2.0)
        still = []
        for cfg, path, proc in running:
            if proc.poll() is None:
                still.append((cfg, path, proc))
            else:
                results[cfg] = path
                print(f"done {cfg} rc={proc.returncode}", flush=True)
        running = still

    base = {r["seed"]: r for r in load(results[base_name])}
    rows = []
    for cfg, path in results.items():
        rs = load(path)
        pairs = [(16 - r["place"], 16 - base[r["seed"]]["place"]) for r in rs if r["seed"] in base]
        if not pairs:
            continue
        score = statistics.mean(p[0] for p in pairs)
        deltas = [p[0] - p[1] for p in pairs]
        sd = statistics.stdev(deltas) if len(deltas) > 1 else 0.0
        wins = sum(1 for r in rs if r["place"] == 1)
        top8 = sum(1 for r in rs if r["place"] <= 8)
        firsts = sum(1 for r in rs if r["place"] >= 15)
        rows.append((cfg, len(pairs), score, statistics.mean(deltas), sd, wins, top8, firsts))

    rows.sort(key=lambda r: -r[2])
    print()
    print(f"{'config':10s} {'n':>4s} {'score':>7s} {'delta':>7s} {'sd':>6s} {'wins':>5s} {'top8':>5s} {'last2':>6s}")
    for cfg, n, score, delta, sd, wins, top8, firsts in rows:
        print(f"{cfg:10s} {n:4d} {score:7.3f} {delta:+7.3f} {sd:6.3f} {wins:5d} {top8:5d} {firsts:6d}")

    summary = {"seed": args.seed, "matches": args.matches,
               "house_vision": args.house_vision,
               "rows": [dict(config=r[0], n=r[1], score=r[2], delta=r[3], sd=r[4],
                             wins=r[5], top8=r[6], last2=r[7]) for r in rows]}
    with open(os.path.join(args.out, f"summary_{args.seed}.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())





