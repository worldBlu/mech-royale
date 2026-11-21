#!/usr/bin/env python3
"""Sweep policy configurations through tools/eval.py and report paired means.

Each configuration is evaluated on the same seed bases, so the comparison against the
reference configuration is paired by seed: the aggregate difference is far less noisy than
comparing two independent 48-match means.

    python tools/sweep.py                 # default 48 matches x 2 seed bases
    python tools/sweep.py --matches 24 --seed 7 --seed 8
"""
import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# name, eval --variant, MF_POLICY_KNOBS payload
CONFIGS = [
    ("base", "", {}),
    ("cover", "", {"cover": 1}),
    ("cubeOff", "", {"cube_chase": 0}),
    ("weaponOff", "", {"weapon_chase": 0}),
    ("coverNoBoost", "no-boost", {"cover": 1}),
    ("coverCubeOff", "", {"cover": 1, "cube_chase": 0}),
    ("noBoost", "no-boost", {}),
    ("noLoot", "no-loot", {}),
]


def run_one(py, name, variant, knobs, matches, seed, out_dir):
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    if knobs:
        env["MF_POLICY_KNOBS"] = json.dumps(knobs)
    else:
        env.pop("MF_POLICY_KNOBS", None)
    out = os.path.join(out_dir, "sweep_%s_%d.json" % (name, seed))
    cmd = [py, os.path.join(HERE, "eval.py"), "--matches", str(matches), "--seed", str(seed),
           "--quiet", "--json-out", out]
    if variant:
        cmd += ["--variant", variant]
    proc = subprocess.run(cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, timeout=3600)
    text = proc.stdout.decode("utf-8", "replace").strip()
    if proc.returncode != 0:
        print("  !! %s seed %d exit %d\n%s" % (name, seed, proc.returncode, text[-2000:]))
        return None
    line = text.splitlines()[-1] if text else ""
    try:
        return json.loads(line)
    except ValueError:
        print("  !! %s seed %d unparsable output: %r" % (name, seed, text[-300:]))
        return None


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--matches", type=int, default=48)
    ap.add_argument("--seed", type=int, action="append", default=None,
                    help="seed base, repeatable (default 1 and 424242)")
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--out", default=os.path.join(ROOT, "reports"))
    args = ap.parse_args()
    seeds = args.seed or [1, 424242]
    os.makedirs(args.out, exist_ok=True)

    results = {}
    for name, variant, knobs in CONFIGS:
        rows = []
        for seed in seeds:
            print("running %-13s seed %-7d ..." % (name, seed))
            res = run_one(args.python, name, variant, knobs, args.matches, seed, args.out)
            if res:
                rows.append((seed, res))
        if not rows:
            continue
        mean = sum(r["mean_score"] for _, r in rows) / len(rows)
        place = sum(r["mean_place"] for _, r in rows) / len(rows)
        surv = sum(r["mean_survival_s"] for _, r in rows) / len(rows)
        kills = sum(r["mean_kills"] for _, r in rows) / len(rows)
        results[name] = {"rows": rows, "mean": mean, "place": place,
                         "survival": surv, "kills": kills}
        print("  -> %-13s mean %.3f  place %.2f  survival %.1fs  kills %.2f"
              % (name, mean, place, surv, kills))

    base = results.get("base")
    if base:
        deltas = []
        print("\npaired deltas vs base (per seed base)")
        for seed, res in base["rows"]:
            for name, data in results.items():
                if name == "base":
                    continue
                other = dict((s, r) for s, r in data["rows"]).get(seed)
                if other:
                    deltas.append((name, other["mean_score"] - res["mean_score"]))
        agg = {}
        for name, d in deltas:
            agg.setdefault(name, []).append(d)
        print("%-15s %8s %8s" % ("config", "delta", "n"))
        for name, ds in sorted(agg.items(), key=lambda kv: -sum(kv[1]) / len(kv[1])):
            print("%-15s %+8.3f %8d" % (name, sum(ds) / len(ds), len(ds)))


if __name__ == "__main__":
    main()
