#!/usr/bin/env python3
"""Protocol conformance check for a mechfight/1 bot. Standard library only.

    python3 check/check.py -- python3 -u python/example_bot.py
    python3 check/check.py -- node ts/example.ts
    python3 check/check.py --map-dir /arena -- docker run --rm -i -v "$PWD/arena:/arena:ro" my-bot

It plays a recorded stream (check/stream.jsonl.gz: a hello, 300 observations from a real match, an
end) to your bot and checks what comes back: `ready` in time, one valid action per observation with
the right tick, answers within the time budget, the newest-observation rule when observations queue
up, debug drawings inside the caps, and a clean exit after `end`. Every message is checked against
mechfight.schema.json.

It is not a game. The observations don't react to your actions, so it says nothing about how well your
bot plays: submit it for a practice score.

Correctness (every message, ticks, the newest-observation rule, exiting) fails on the first pass. Missing the
time budget for more than 2% of observations is retried with a fresh bot, up to 3 passes, and the best
counts: a busy machine makes a good bot late now and then, a slow bot is late every time.
"""
import argparse
import gzip
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
KIT = os.path.dirname(HERE)


# ------------------------------------------------------------------ a small JSON Schema validator
# Enough of draft-07 for mechfight.schema.json: type, const, enum, properties, required,
# additionalProperties, items, minItems, maxItems, anyOf, oneOf, pattern, $ref.
class Schema:
    def __init__(self, path):
        with open(path) as f:
            self.root = json.load(f)

    def errors(self, value, name):
        out = []
        self._check(value, {"$ref": "#/definitions/" + name}, "$", out)
        return out

    def _type_ok(self, v, t):
        return {
            "object": isinstance(v, dict),
            "array": isinstance(v, list),
            "string": isinstance(v, str),
            "boolean": isinstance(v, bool),
            "null": v is None,
            "number": isinstance(v, (int, float)) and not isinstance(v, bool),
            "integer": isinstance(v, int) and not isinstance(v, bool),
        }.get(t, True)

    def _check(self, v, s, at, out):
        if len(out) > 5:
            return
        if "$ref" in s:
            s = self.root["definitions"][s["$ref"].split("/")[-1]]
            return self._check(v, s, at, out)
        if "anyOf" in s or "oneOf" in s:
            alts = s.get("anyOf") or s.get("oneOf")
            if not any(not self._sub(v, a, at) for a in alts):
                out.append(f"{at}: matches none of the allowed shapes")
            return
        t = s.get("type")
        if t is not None:
            ts = t if isinstance(t, list) else [t]
            if not any(self._type_ok(v, x) for x in ts):
                out.append(f"{at}: expected {' or '.join(ts)}, got {type(v).__name__}")
                return
        if "const" in s and v != s["const"]:
            out.append(f"{at}: expected {json.dumps(s['const'])}")
        if "enum" in s and v not in s["enum"]:
            out.append(f"{at}: expected one of {s['enum']}")
        if "pattern" in s and isinstance(v, str) and not re.search(s["pattern"], v):
            out.append(f"{at}: {v!r} doesn't match {s['pattern']}")
        if isinstance(v, dict):
            props = s.get("properties", {})
            for k in s.get("required", []):
                if k not in v:
                    out.append(f"{at}: missing {k!r}")
            for k, x in v.items():
                if k in props:
                    self._check(x, props[k], f"{at}.{k}", out)
                elif isinstance(s.get("additionalProperties"), dict):
                    self._check(x, s["additionalProperties"], f"{at}.{k}", out)
        if isinstance(v, list):
            if "minItems" in s and len(v) < s["minItems"]:
                out.append(f"{at}: needs at least {s['minItems']} items")
            if "maxItems" in s and len(v) > s["maxItems"]:
                out.append(f"{at}: at most {s['maxItems']} items")
            items = s.get("items")
            for i, x in enumerate(v):
                it = items[i] if isinstance(items, list) and i < len(items) else items
                if isinstance(it, dict):
                    self._check(x, it, f"{at}[{i}]", out)

    def _sub(self, v, s, at):
        out = []
        self._check(v, s, at, out)
        return out


# ------------------------------------------------------------------ talking to the bot
class Bot:
    def __init__(self, cmd):
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, bufsize=0)
        self.lines = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for raw in self.proc.stdout:
            self.lines.put((time.perf_counter(), raw))
        self.lines.put((time.perf_counter(), None))

    def send(self, msg):
        self.send_many([msg])

    def send_many(self, msgs):
        """One write, so queued observations arrive together."""
        try:
            self.proc.stdin.write("".join(json.dumps(m, separators=(",", ":")) + "\n" for m in msgs).encode())
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError):
            pass

    def next(self, deadline):
        """Next line before `deadline` (perf_counter): (time, raw bytes), (time, None) on exit, None on timeout."""
        try:
            return self.lines.get(timeout=max(0.0, deadline - time.perf_counter()))
        except queue.Empty:
            return None


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], usage="%(prog)s [options] -- <bot command...>")
    ap.add_argument("--stream", default=os.path.join(HERE, "stream.jsonl.gz"))
    ap.add_argument("--schema", default=os.path.join(KIT, "mechfight.schema.json"))
    ap.add_argument("--map-dir", default=os.path.join(KIT, "arena"), help="what hello.map.dir says (default: the kit's arena/)")
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    a = ap.parse_args()
    cmd = a.cmd[1:] if a.cmd[:1] == ["--"] else a.cmd
    if not cmd:
        ap.error("give the bot command after --")
    schema = Schema(a.schema)
    with gzip.open(a.stream, "rt") as f:
        msgs = [json.loads(l) for l in f if l.strip()]
    hello, obs, end = msgs[0], [m for m in msgs if m.get("type") == "obs"], msgs[-1]
    hello["map"]["dir"] = os.path.abspath(a.map_dir)
    cfg = hello["config"]
    budget = cfg["bot"]["tick_budget_ms"] / 1000
    caps = cfg["debug"]
    late_limit = 0.02

    def run(attempt):
        """One pass over the stream with a fresh bot: (problems, warnings, counts, times, stats line)."""
        problems, warnings, times = [], [], []
        counts = {"answered": 0, "late": 0, "missing": 0, "stale": 0, "wrong_tick": 0, "bad": 0, "debug": 0, "debug_over_caps": 0}
        sent_ticks = set()

        def problem(s):
            if len(problems) < 30:
                problems.append(s)

        def parse(raw, what):
            if len(raw.rstrip(b"\n")) > cfg["bot"]["max_line_bytes"]:
                counts["bad"] += 1
                problem(f"{what}: line over {cfg['bot']['max_line_bytes']} bytes")
                return None
            try:
                m = json.loads(raw)
            except ValueError:
                counts["bad"] += 1
                problem(f"{what}: not JSON: {raw[:80]!r}")
                return None
            if not isinstance(m, dict):
                counts["bad"] += 1
                problem(f"{what}: not a JSON object")
                return None
            return m

        if attempt == 1:
            print(f"bot: {' '.join(cmd)}")
        bot = Bot(cmd)
        t0 = time.perf_counter()
        bot.send(hello)
        ready = None
        while ready is None:
            got = bot.next(t0 + cfg["bot"]["hello_budget_ms"] / 1000)
            if got is None or got[1] is None:
                problems.append(f"no `ready` within {cfg['bot']['hello_budget_ms']} ms" if got is None else "the bot exited before `ready`")
                bot.proc.kill()
                return problems, warnings, counts, times, None
            m = parse(got[1], "ready")
            if m is not None and m.get("type") == "ready":
                ready = m
        for e in schema.errors(ready, "Ready"):
            problem(f"ready: {e}")
        if attempt == 1:
            print(f"ready in {(time.perf_counter() - t0) * 1000:.0f} ms: name {ready.get('name')!r}")

        def check_action(m, o):
            for e in schema.errors(m, "Action"):
                problem(f"tick {o['tick']}: {e}")
            d = m.get("debug")
            if isinstance(d, dict):
                counts["debug"] += 1
                over = [k for k, c in (("lines", "max_lines"), ("points", "max_points"), ("text", "max_text")) if len(d.get(k) or []) > caps[c]]
                if len(json.dumps(d, separators=(",", ":")).encode()) > caps["max_bytes"]:
                    over.append("bytes")
                if any(len(t.get("text", "")) > caps["max_text_chars"] for t in d.get("text") or [] if isinstance(t, dict)) or len(str(d.get("label", ""))) > caps["max_text_chars"]:
                    over.append("text length")
                if over:
                    counts["debug_over_caps"] += 1
                    if counts["debug_over_caps"] <= 3:
                        warnings.append(f"tick {o['tick']}: debug drawing over the caps ({', '.join(over)}): the server cuts it")

        def ask(batch):
            """Send observations back to back; the answer that counts is the one for the last."""
            want = batch[-1]
            sent = time.perf_counter()
            sent_ticks.update(o["tick"] for o in batch)
            bot.send_many(batch)
            while True:
                got = bot.next(sent + budget)
                if got is None:
                    counts["missing"] += 1
                    # A late answer still arrives: drain it so it isn't read as the next one.
                    late = bot.next(sent + budget + 0.25)
                    if late is not None and late[1] is not None:
                        counts["late"] += 1
                        times.append(late[0] - sent)
                        m = parse(late[1], f"tick {want['tick']} (late)")
                        if m is not None and m.get("tick") == want["tick"]:
                            check_action(m, want)
                    return True
                at, raw = got
                if raw is None:
                    problem(f"the bot exited at tick {want['tick']}")
                    return False
                m = parse(raw, f"tick {want['tick']}")
                if m is None:
                    continue
                if m.get("tick") != want["tick"]:
                    if m.get("tick") in sent_ticks:
                        # an answer to an older observation (queued, or one it answered too late): dropped, a timing matter
                        counts["stale"] += 1
                    else:
                        counts["wrong_tick"] += 1
                        problem(f"tick {want['tick']}: answer carries tick {m.get('tick')!r}")
                    continue
                counts["answered"] += 1
                times.append(at - sent)
                check_action(m, want)
                return True

        alive = True
        for i, o in enumerate(obs):
            # Every 25th observation, three arrive at once (as when a bot falls behind): answer the newest.
            batch = obs[max(0, i - 2) : i + 1] if i % 25 == 24 else [o]
            if not ask(batch):
                alive = False
                break
            # No answer at all, not even a late one: the bot is stuck, not slow.
            if counts["missing"] - counts["late"] + counts["wrong_tick"] >= 20:
                problem("stopped early: 20 observations without any answer")
                break
        if alive:
            bot.send(end)
            try:
                bot.proc.stdin.close()
            except OSError:
                pass
            try:
                bot.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                warnings.append("the bot didn't exit within 2 s of `end` (the server kills it)")
                bot.proc.kill()
        else:
            bot.proc.kill()
        n = len(obs)
        stats = f"observations: {n}  answered in time: {counts['answered']}  missed the {budget * 1000:.0f} ms budget: {counts['missing']} ({counts['late']} came late)"
        return problems, warnings, counts, sorted(times), stats

    # Correctness first: anything wrong in what the bot says fails at once. Only lateness is retried: on a busy
    # machine (a scorer running other matches) a good bot can miss the budget now and then, while a slow bot
    # misses it every time. Up to 3 passes, each with a fresh bot; the best one counts.
    n = len(obs)
    for attempt in range(1, 4):
        problems, warnings, counts, times, stats = run(attempt)
        slow = stats is not None and counts["missing"] > n * late_limit
        if problems or not slow or attempt == 3:
            break
        print(f"pass {attempt}: {counts['missing']} of {n} observations missed the {budget * 1000:.0f} ms budget; this machine may be busy, running the stream again with a fresh bot")
    if stats is None:
        print(f"FAIL: {problems[0]}")
        return 1
    pct = lambda p: times[min(len(times) - 1, int(p * len(times)))] * 1000 if times else 0.0
    if attempt > 1:
        print(f"pass {attempt} of 3:")
    print(stats)
    print(f"answer time ms: p50 {pct(0.5):.1f}  p90 {pct(0.9):.1f}  p99 {pct(0.99):.1f}  max {pct(1.0):.1f}  (this machine, not the 1-CPU match container)")
    print(f"answers to older queued observations (dropped): {counts['stale']}  wrong tick: {counts['wrong_tick']}  bad lines: {counts['bad']}  debug drawings: {counts['debug']} ({counts['debug_over_caps']} over the caps)")
    if slow:
        problems.append(f"{counts['missing']} of {n} observations got no answer within {budget * 1000:.0f} ms" + (f" (best of {attempt} passes)" if attempt > 1 else ""))
    for w in warnings:
        print(f"warning: {w}")
    for p in problems:
        print(f"problem: {p}")
    print("FAIL" if problems else "PASS")
    return 1 if problems else 0

if __name__ == "__main__":
    sys.exit(main())
