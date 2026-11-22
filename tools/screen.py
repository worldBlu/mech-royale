"""Parallel policy screen: run several knob sets on the same seeds, compare paired scores.

  python tools/screen.py --matches 48 --seeds 1,424242 --jobs 8 \
      --house-preset harsh --house-vision vision --out reports/screen_harsh

Each config runs ``tools/eval.py --quiet --json-out`` in its own process with
``MF_POLICY_KNOBS`` set, so one config cannot leak state into another.  The first config
is the baseline; every other config is reported as a paired delta on the same seeds.

Rows: config, n, mean delta, sd of the per-seed deltas, per-seed deltas, and whether the
sign is consistent across seeds.
"""
import argparse
import json
import os
import shlex
import statistics
import subprocess
import sys
import time

CONFIGS = [
    ("base", {}),
    ("target_weak1", {"target_weak": 1}),
    ("camp1", {"camp": 1}),
    ("chase1", {"chase": 1}),
]


def load_configs(path):
    if not path:
        return list(CONFIGS)
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, dict):
        return [(str(k), dict(v or {})) for k, v in data.items()]
    out = []
    for item in data:
        if isinstance(item, (list, tuple)):
            out.append((str(item[0]), dict(item[1] or {})))
        else:
            out.append((str(item["name"]), dict(item.get("knobs") or {})))
    return out


def eval_cmd(args, seed, path, knobs):
    env = dict(os.environ)
    env["MF_POLICY_KNOBS"] = json.dumps(knobs)
    cmd = [args.python, "tools/eval.py", "--matches", str(args.matches),
           "--seed", str(seed), "--quiet", "--json-out", path,
           "--house-vision", args.house_vision, "--house-preset", args.house_preset]
    if args.bot_dir:
        cmd += ["--bot-dir", args.bot_dir]
    for kv in args.house_sense:
        cmd += ["--house-sense", kv]
    cmd += shlex.split(args.extra or "")
    return cmd, env


def read_results(path):
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, dict):
        return data.get("results") or data.get("matches") or []
    return data


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--matches", type=int, default=48)
    ap.add_argument("--seeds", default="1")
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--out", default="reports/screen1")
    ap.add_argument("--configs-file", default="")
    ap.add_argument("--only", default="")
    ap.add_argument("--house-vision", default="vision", choices=("pings", "vision"))
    ap.add_argument("--house-preset", default="calibrated", choices=("calibrated", "harsh"))
    ap.add_argument("--house-sense", action="append", default=[])
    ap.add_argument("--bot-dir", default="")
    ap.add_argument("--extra", default="")
    ap.add_argument("--python", default=sys.executable)
    args = ap.parse_args()

    seeds = [int(s) for s in args.seeds.split(",") if s.strip()]
    configs = load_configs(args.configs_file)
    if args.only:
        wanted = {n.strip() for n in args.only.split(",") if n.strip()}
        configs = [c for c in configs if c[0] in wanted]
    if not configs:
        print("no configs selected", file=sys.stderr)
        return 2
    base_name = configs[0][0]
    os.makedirs(args.out, exist_ok=True)

    tasks = [(name, knobs, seed) for (name, knobs) in configs for seed in seeds]
    pending = list(tasks)
    running = []
    paths = {}
    fails = []
    t0 = time.time()
    while pending or running:
        while pending and len(running) < args.jobs:
            name, knobs, seed = pending.pop(0)
            path = os.path.join(args.out, f"{name}_{seed}.json")
            cmd, env = eval_cmd(args, seed, path, knobs)
            running.append((name, seed, path,
                            subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL,
                                             stderr=subprocess.DEVNULL)))
        time.sleep(1.0)
        still = []
        for name, seed, path, proc in running:
            if proc.poll() is None:
                still.append((name, seed, path, proc))
                continue
            if proc.returncode == 0:
                paths[(name, seed)] = path
            else:
                fails.append((name, seed, proc.returncode))
            print(f"done {name} seed={seed} rc={proc.returncode}", flush=True)
        running = still

    per_seed = {}   # (name, seed) -> mean score
    stats = {}      # name -> aggregated
    for (name, seed), path in paths.items():
        rs = read_results(path)
        scores = [16.0 - float(r["place"]) for r in rs]
        if not scores:
            continue
        per_seed[(name, seed)] = statistics.mean(scores)
        st = stats.setdefault(name, {"n": 0, "wins": 0, "top8": 0, "last2": 0,
                                     "surv": [], "places": []})
        st["n"] += len(scores)
        st["wins"] += sum(1 for s in scores if s == 15.0)
        st["top8"] += sum(1 for s in scores if s >= 8.0)
        st["last2"] += sum(1 for s in scores if s <= 1.0)
        st["surv"].extend(float(r.get("t") or 0.0) for r in rs)

    rows = []
    for name, _knobs in configs:
        deltas = []
        seed_scores = {}
        seed_deltas = {}
        for seed in seeds:
            if (name, seed) not in per_seed or (base_name, seed) not in per_seed:
                continue
            s = per_seed[(name, seed)]
            b = per_seed[(base_name, seed)]
            seed_scores[seed] = s
            seed_deltas[seed] = s - b
            deltas.append(s - b)
        if not deltas:
            continue
        st = stats.get(name, {})
        rows.append({
            "config": name,
            "n": st.get("n", 0),
            "scores": seed_scores,
            "deltas": seed_deltas,
            "mean_delta": statistics.mean(deltas),
            "sd_delta": statistics.stdev(deltas) if len(deltas) > 1 else 0.0,
            "consistent": all(d > 0 for d in deltas) or all(d < 0 for d in deltas),
            "wins": st.get("wins", 0),
            "top8": st.get("top8", 0),
            "last2": st.get("last2", 0),
            "surv": statistics.mean(st["surv"]) if st.get("surv") else 0.0,
        })

    rows.sort(key=lambda r: -r["mean_delta"])
    print()
    print(f"preset={args.house_preset} vision={args.house_vision} seeds={seeds} "
          f"matches/seed={args.matches} base={base_name}")
    head = f"{'config':14s} {'n':>4s} {'mean_d':>7s} {'sd_d':>6s} {'sign':>5s}"
    for seed in seeds:
        head += f" {'d@'+str(seed):>10s} {'s@'+str(seed):>8s}"
    head += f" {'wins':>5s} {'top8':>5s} {'last2':>6s}"
    print(head)
    for r in rows:
        line = (f"{r['config']:14s} {r['n']:4d} {r['mean_delta']:+7.3f} "
                f"{r['sd_delta']:6.3f} {'yes' if r['consistent'] else 'NO':>5s}")
        for seed in seeds:
            d = r["deltas"].get(seed)
            s = r["scores"].get(seed)
            line += f" {d:+10.3f}" if d is not None else f" {'-':>10s}"
            line += f" {s:8.3f}" if s is not None else f" {'-':>8s}"
        line += f" {r['wins']:5d} {r['top8']:5d} {r['last2']:6d}"
        print(line)
    print(f"\nwall {time.time() - t0:.0f}s, {len(paths)} evals"
          + (f", {len(fails)} failed" if fails else ""))

    summary = {"seeds": seeds, "matches": args.matches, "base": base_name,
               "house_vision": args.house_vision, "house_preset": args.house_preset,
               "house_sense": args.house_sense, "bot_dir": args.bot_dir,
               "rows": rows, "failed": fails}
    with open(os.path.join(args.out, "screen_summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
