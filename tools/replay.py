#!/usr/bin/env python3
"""In-process replay of a recorded Mech Royale stream (``kit/check/stream.jsonl.gz``).

The recorded observations contain the *original* bot's torso aim, ammo, cooldowns and
positions, so feeding them straight into our bot measures the wrong thing: our bot would
be shooting along somebody else's aim.  This tool therefore replays the stream against a
small, explicitly-documented "own mech" simulation:

* torso aim slews toward the commanded aim at ``turn_rate`` / ``pitch_rate`` (2 ticks per
  decision, 0.05 s each, controls applied before weapons inside a tick);
* our own gun/laser/mortar/lightning state (ammo, reload, cooldown, charge, energy) is
  simulated from the fire flags our bot sends, not copied from the recording;
* every simulated shot is flown forward with the protocol's projectile speed / gravity
  against the *recorded* target trajectories, so hit rate and miss distance are measured
  rather than guessed;
* aim error at the moment of firing is measured against the ideal solution for that
  moment (not against the recorded bot's aim).

What cannot be simulated from a recording: the world does not react to us.  Our movement
does not change the recorded self position (the recorded path is followed), and visibility
(`obs.mechs`) is whatever the original bot could see.  Those are documented limitations;
they matter for distance/zone numbers, not for aim error at fire time.

Usage:
    python tools/replay.py                       # full summary
    python tools/replay.py --trace               # + one line per match second
    python tools/replay.py --arm laser           # what-if: give us gun + laser
    python tools/replay.py --json out.json       # machine-readable summary
    python tools/replay.py --no-sim              # feed obs unchanged (raw recorded aim)

Stdlib only.
"""
import argparse
import bisect
import gzip
import json
import math
import os
import sys
import time
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOT_DIR = os.path.join(ROOT, "bot")
if BOT_DIR not in sys.path:
    sys.path.insert(0, BOT_DIR)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

TAU = 2.0 * math.pi


# --------------------------------------------------------------------------- helpers
def ang_diff(a, b):
    return (a - b + math.pi) % TAU - math.pi


def clamp(v, lo, hi):
    return lo if v < lo else (hi if v > hi else v)


def dir_from(yaw, pitch):
    cp = math.cos(pitch)
    return (cp * math.cos(yaw), cp * math.sin(yaw), math.sin(pitch))


def angle_between(a, b):
    d = a[0] * b[0] + a[1] * b[1] + a[2] * b[2]
    return math.acos(clamp(d, -1.0, 1.0))


def quantile(sorted_vals, q):
    if not sorted_vals:
        return 0.0
    idx = int(round(q * (len(sorted_vals) - 1)))
    return sorted_vals[idx]


class Dist:
    """Small percentile accumulator (no numpy: bot tools are stdlib only)."""

    def __init__(self):
        self.vals = []

    def add(self, v):
        if v is not None and math.isfinite(v):
            self.vals.append(float(v))

    def summary(self):
        if not self.vals:
            return {"n": 0}
        s = sorted(self.vals)
        return {
            "n": len(s),
            "mean": sum(s) / len(s),
            "p50": quantile(s, 0.50),
            "p90": quantile(s, 0.90),
            "p99": quantile(s, 0.99),
            "max": s[-1],
        }

    def fmt(self, unit="", digits=2):
        d = self.summary()
        if not d.get("n"):
            return "n=0"
        return ("n=%d p50=%.{d}f p90=%.{d}f p99=%.{d}f max=%.{d}f%s"
                .format(d=digits) % (d["n"], d["p50"], d["p90"], d["p99"], d["max"], unit))


# --------------------------------------------------------------------------- stream
def load_stream(path):
    opener = gzip.open if path.endswith(".gz") else open
    msgs = []
    with opener(path, "rt", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            msgs.append(json.loads(line))
    return msgs


def resolve_map_dir(hello, stream_path, override):
    if override:
        return os.path.abspath(override)
    raw = ((hello.get("map") or {}).get("dir")) or "/arena"
    cands = []
    if os.path.isabs(raw):
        cands.append(raw)
    cands.append(os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(stream_path)), raw)))
    cands.append(os.path.normpath(os.path.join(ROOT, raw)))
    cands.append(os.path.join(ROOT, "kit", "arena"))
    for c in cands:
        if os.path.isfile(os.path.join(c, "v2", "world.json")):
            return c
    raise SystemExit("replay: cannot find the arena (tried: %s)" % ", ".join(cands))


# --------------------------------------------------------------------------- torso
class Torso:
    """The server's aim slewing, driven by the aim commands we actually send."""

    def __init__(self, aim, stats):
        self.yaw = float(aim[0])
        self.pitch = float(aim[1])
        self.set_stats(stats)

    def set_stats(self, stats):
        stats = stats or {}
        self.turn = float(stats.get("turn_rate", 4.0))
        self.pitch_rate = float(stats.get("pitch_rate", 3.0))
        self.min_pitch = float(stats.get("min_pitch", -0.7))
        self.max_pitch = float(stats.get("max_pitch", 1.3))

    def slew(self, cmd, dt=0.05):
        if not cmd:
            return
        dy = ang_diff(float(cmd[0]), self.yaw)
        dp = float(cmd[1]) - self.pitch
        self.yaw = (self.yaw + clamp(dy, -self.turn * dt, self.turn * dt) + math.pi) % TAU - math.pi
        self.pitch = clamp(self.pitch + clamp(dp, -self.pitch_rate * dt, self.pitch_rate * dt),
                           self.min_pitch, self.max_pitch)

    def aim(self):
        return [self.yaw, self.pitch]

    def direction(self):
        return dir_from(self.yaw, self.pitch)


# --------------------------------------------------------------------------- weapons
class SimWeapons:
    """Own weapon state machine: ammo/reload/cooldown/charge/lightning energy.

    It accepts exactly what the server accepts (a hold-trigger flag per slot and a reload
    flag) and returns the shots that would actually leave in a 0.05 s tick.
    """

    def __init__(self, obs_self, cfg, arm=None):
        self.cfg = cfg or {}
        cw = self.cfg.get("weapons") or {}
        self.wcfg = cw
        self.slots = []
        src = list(obs_self.get("weapons") or [])
        slot0 = src[0] if src else None
        self.slots.append(self._from_obs(slot0) if slot0 else None)
        slot1 = src[1] if len(src) > 1 else None
        if arm:
            self.slots.append(self._from_kind(arm))
        elif slot1:
            self.slots.append(self._from_obs(slot1))
        else:
            self.slots.append(None)
        while len(self.slots) < 2:
            self.slots.append(None)
        self.reloads = 0
        self.swaps = 0

    # -- construction -----------------------------------------------------------------
    def _stats_for(self, kind, given=None):
        st = dict((self.wcfg.get(kind) or {}))
        if given:
            st.update(given)
        return st

    def _from_obs(self, w):
        kind = w.get("kind")
        st = self._stats_for(kind, w.get("stats"))
        return {
            "kind": kind,
            "stats": st,
            "ammo": float(w.get("ammo", st.get("mag", 0))),
            "reload": float(w.get("reload", 0.0)),
            "cooldown": float(w.get("cooldown", 0.0)),
            "charge": float(w.get("charge", 0.0)),
            "hold": 0.0,
        }

    def _from_kind(self, kind):
        st = self._stats_for(kind)
        return {
            "kind": kind,
            "stats": st,
            "ammo": float(st.get("mag", 0)),
            "reload": 0.0,
            "cooldown": 0.0,
            "charge": 0.0,
            "hold": 0.0,
        }

    # -- observations -----------------------------------------------------------------
    def public(self):
        out = []
        for w in self.slots:
            if not w:
                out.append(None)
                continue
            out.append({
                "kind": w["kind"],
                "stats": dict(w["stats"]),
                "ammo": w["ammo"],
                "reload": w["reload"],
                "cooldown": w["cooldown"],
                "charge": w["charge"],
            })
        return out

    def kinds(self):
        return [w["kind"] if w else None for w in self.slots]

    # -- simulation -------------------------------------------------------------------
    def tick(self, dt, fire, reload_req, energy, aim, pos, now):
        """Advance one 0.05 s tick.  Returns (shots, energy_spent)."""
        fire = list(fire or [])
        shots = []
        spent = 0.0
        for i, w in enumerate(self.slots):
            if not w:
                continue
            kind = w["kind"]
            st = w["stats"]
            w["cooldown"] = max(0.0, w["cooldown"] - dt)
            trigger = bool(fire[i]) if i < len(fire) else False
            if kind == "gun":
                if w["reload"] > 0.0:
                    w["reload"] -= dt
                    if w["reload"] <= 0.0:
                        w["reload"] = 0.0
                        w["ammo"] = float(st.get("mag", 12))
                if reload_req and w["ammo"] < float(st.get("mag", 12)) and w["reload"] <= 0.0:
                    w["reload"] = float(st.get("reload", 2.4))
                    self.reloads += 1
                if trigger and w["ammo"] > 0.0 and w["cooldown"] <= 0.0 and w["reload"] <= 0.0:
                    w["ammo"] -= 1.0
                    w["cooldown"] = float(st.get("cooldown", 0.24))
                    shots.append(self._shot(kind, i, aim, pos, now, st))
                    if w["ammo"] <= 0.0 and w["reload"] <= 0.0:
                        w["reload"] = float(st.get("reload", 2.4))
                        self.reloads += 1
            elif kind == "laser":
                cmax = float(st.get("charge", 0.75))
                hold_s = float(st.get("hold_s", 2.0))
                if trigger:
                    if w["cooldown"] <= 0.0:
                        if w["charge"] < cmax:
                            w["charge"] = min(cmax, w["charge"] + dt)
                            w["hold"] = 0.0
                        else:
                            w["charge"] = cmax
                            w["hold"] += dt
                        if w["charge"] >= cmax and w["hold"] >= hold_s:
                            shots.append(self._shot(kind, i, aim, pos, now, st,
                                                    charge=w["charge"]))
                            w["charge"] = 0.0
                            w["hold"] = 0.0
                            w["cooldown"] = float(st.get("cooldown", 1.65))
                else:
                    if w["charge"] > 0.0:
                        # let go: fires at the charge (min_charge is enforced by the server)
                        shots.append(self._shot(kind, i, aim, pos, now, st,
                                                charge=max(w["charge"], float(st.get("min_charge", 0.15)))))
                        w["charge"] = 0.0
                        w["hold"] = 0.0
                        w["cooldown"] = float(st.get("cooldown", 1.65))
            elif kind == "mortar":
                if trigger and w["cooldown"] <= 0.0:
                    w["cooldown"] = float(st.get("cooldown", 2.6))
                    shots.append(self._shot(kind, i, aim, pos, now, st))
            elif kind == "lightning":
                cost = float(st.get("energy", 3.0))
                if trigger and w["cooldown"] <= 0.0 and energy >= cost:
                    w["cooldown"] = float(st.get("cooldown", 0.25))
                    energy -= cost
                    spent += cost
                    shots.append(self._shot(kind, i, aim, pos, now, st))
        return shots, spent

    def _shot(self, kind, slot, aim, pos, now, st, charge=None):
        yaw, pitch = float(aim[0]), float(aim[1])
        muz = float(st.get("muzzle_height", 7.0))
        if kind == "mortar":
            muz += 2.5      # mortar tube: pos.z + 9.5
        mx, my, mz = float(pos[0]), float(pos[1]), float(pos[2]) + muz
        d = dir_from(yaw, pitch)
        origin = (mx + d[0] * 3.0, my + d[1] * 3.0, mz + d[2] * 3.0)
        return {
            "kind": kind,
            "slot": slot,
            "t": now,
            "pos": (float(pos[0]), float(pos[1]), float(pos[2])),
            "origin": origin,
            "aim": (yaw, pitch),
            "dir": d,
            "stats": st,
            "charge": charge,
            "target": None,
            "crate": False,
        }


# --------------------------------------------------------------------------- timeline
class TargetTrack:
    __slots__ = ("aid", "kind", "samples", "_times")

    def __init__(self, aid, kind):
        self.aid = aid
        self.kind = kind
        self.samples = []        # (t, x, y, z, vx, vy, vz)
        self._times = []

    def add(self, t, pos, vel):
        if self._times and abs(t - self._times[-1]) < 1e-9:
            self.samples[-1] = (t, pos[0], pos[1], pos[2], vel[0], vel[1], vel[2])
            return
        self._times.append(t)
        self.samples.append((t, pos[0], pos[1], pos[2], vel[0], vel[1], vel[2]))

    def at(self, time, horizon=0.35):
        """Interpolated (pos, vel) at ``time`` or None when the track is too stale."""
        if not self._times:
            return None
        i = bisect.bisect_right(self._times, time) - 1
        if i < 0:
            return None
        s = self.samples[i]
        if i + 1 < len(self.samples):
            s2 = self.samples[i + 1]
            if s2[0] - s[0] <= 0.6:
                span = s2[0] - s[0]
                f = (time - s[0]) / span
                p = (s[1] + (s2[1] - s[1]) * f, s[2] + (s2[2] - s[2]) * f,
                     s[3] + (s2[3] - s[3]) * f)
                v = (s[4], s[5], s[6])
                return p, v
        dt = time - s[0]
        if dt < 0.0 or dt > horizon:
            return None
        return ((s[1] + s[4] * dt, s[2] + s[5] * dt, s[3] + s[6] * dt),
                (s[4], s[5], s[6]))

    def last_time(self):
        return self._times[-1] if self._times else -1.0


class Timeline:
    def __init__(self, obs_list):
        self.tracks = {}
        self.self_samples = []    # (t, x, y, z)
        self.self_times = []
        self.self_stats = []
        self._build(obs_list)

    def _build(self, obs_list):
        for obs in obs_list:
            t = float(obs["t"])
            for m in obs.get("mechs") or []:
                aid = int(m["id"])
                tr = self.tracks.get(aid)
                if tr is None:
                    tr = TargetTrack(aid, m.get("kind", "mech"))
                    self.tracks[aid] = tr
                tr.add(t, m["pos"], m.get("vel") or (0.0, 0.0, 0.0))
            s = obs["self"]
            pos = s["pos"]
            if self.self_times and abs(t - self.self_times[-1]) < 1e-9:
                self.self_samples[-1] = (t, pos[0], pos[1], pos[2])
            else:
                self.self_times.append(t)
                self.self_samples.append((t, pos[0], pos[1], pos[2]))
                self.self_stats.append(s.get("stats") or {})

    def get(self, aid):
        return self.tracks.get(int(aid)) if aid is not None else None

    def ids(self):
        return list(self.tracks.keys())

    def self_at(self, time):
        if not self.self_times:
            return None
        i = bisect.bisect_right(self.self_times, time) - 1
        if i < 0:
            return None
        if i + 1 < len(self.self_samples):
            s, s2 = self.self_samples[i], self.self_samples[i + 1]
            span = s2[0] - s[0]
            if span > 1e-9:
                f = clamp((time - s[0]) / span, 0.0, 1.0)
                return (s[1] + (s2[1] - s[1]) * f, s[2] + (s2[2] - s[2]) * f,
                        s[3] + (s2[3] - s[3]) * f)
        return (self.self_samples[i][1], self.self_samples[i][2], self.self_samples[i][3])

    def self_stats_at(self, time):
        if not self.self_times:
            return {}
        i = bisect.bisect_right(self.self_times, time) - 1
        return self.self_stats[max(0, i)]


# --------------------------------------------------------------------------- hit sim
class HitSim:
    """Fly a simulated shot forward against the recorded target trajectories."""

    def __init__(self, world, timeline, cfg):
        self.w = world
        self.tl = timeline
        self.cfg = cfg or {}
        self.terrain_ok = hasattr(world, "terrain_z")
        mech = self.cfg.get("mech") or {}

    def ground(self, x, y):
        if not self.terrain_ok:
            return 0.0
        try:
            return float(self.w.terrain_z(x, y))
        except Exception:
            return 0.0

    @staticmethod
    def dims(kind):
        if kind == "npc":
            return 1.8, 3.5
        return 2.5, 9.0

    def cylinder_hit(self, p, tp, kind, pad=0.0):
        r, h = self.dims(kind)
        if (p[0] - tp[0]) ** 2 + (p[1] - tp[1]) ** 2 > (r + pad) ** 2:
            return False
        return tp[2] - 0.1 <= p[2] <= tp[2] + h + 0.1

    def nearest_targets(self, time, exclude=None):
        out = []
        for aid, tr in self.tl.tracks.items():
            if aid == exclude:
                continue
            got = tr.at(time)
            if got:
                out.append((aid, tr.kind, got[0]))
        return out

    def shell_or_bullet(self, shot, time):
        kind = shot["kind"]
        st = shot["stats"]
        speed = float(st.get("speed", 252.0 if kind == "gun" else 72.0))
        gravity = 0.0 if kind == "gun" else float(st.get("gravity", 22.0))
        rng = float(st.get("range", 230.0 if kind == "gun" else 235.0))
        ttl = float(st.get("ttl", 7.0))
        origin = shot["origin"]
        d = shot["dir"]
        max_t = min(ttl, rng / max(1.0, speed) + 0.5) if kind == "gun" else ttl
        dt = 0.01 if kind == "gun" else 0.02
        intended = shot["target"]
        intended_tr = self.tl.get(int(intended)) if intended is not None else None
        best_miss = None
        best_t = None
        impact = None
        t = 0.0
        first_time = shot["t"]
        while t <= max_t:
            p = (origin[0] + d[0] * speed * t,
                 origin[1] + d[1] * speed * t,
                 origin[2] + d[2] * speed * t - 0.5 * gravity * t * t)
            if kind == "gun":
                if (p[0] - origin[0]) ** 2 + (p[1] - origin[1]) ** 2 > rng * rng:
                    break
            if p[2] <= self.ground(p[0], p[1]):
                impact = p
                break
            for aid, tkind, tp in self.nearest_targets(first_time + t):
                if self.cylinder_hit(p, tp, tkind):
                    if kind == "gun":
                        return {"outcome": "hit", "aid": aid, "t_hit": t,
                                "miss_m": 0.0, "impact": p}
                    impact = p
                    break
            if impact is not None:
                break
            if intended_tr is not None:
                got = intended_tr.at(first_time + t)
                if got:
                    r, h = self.dims(intended_tr.kind)
                    tp = got[0]
                    dz = 0.0 if tp[2] <= p[2] <= tp[2] + h else (
                        tp[2] - p[2] if p[2] < tp[2] else p[2] - (tp[2] + h))
                    dh = max(0.0, math.hypot(p[0] - tp[0], p[1] - tp[1]) - r)
                    miss = math.hypot(dh, dz)
                    if best_miss is None or miss < best_miss:
                        best_miss = miss
                        best_t = t
            t += dt
        if impact is None:
            outcome = "miss"
            if intended_tr is not None and best_t is not None:
                if intended_tr.at(first_time + best_t) is None:
                    outcome = "untracked"
            return {"outcome": outcome, "aid": None, "t_hit": None,
                    "miss_m": best_miss, "impact": None}
        if kind == "gun":
            outcome = "miss_ground"
            if intended_tr is not None:
                got = intended_tr.at(first_time + t)
                if got is None:
                    outcome = "untracked"
            return {"outcome": outcome, "aid": None, "t_hit": None,
                    "miss_m": best_miss, "impact": impact}
        # mortar: splash
        splash_r = float(st.get("splash_radius", 7.0))
        nearest = None
        for aid, tkind, tp in self.nearest_targets(first_time + t):
            r, h = self.dims(tkind)
            c = (tp[0], tp[1], tp[2] + h * 0.5)
            dist = math.sqrt((c[0] - impact[0]) ** 2 + (c[1] - impact[1]) ** 2 +
                             (c[2] - impact[2]) ** 2)
            if dist <= splash_r and (nearest is None or dist < nearest[1]):
                nearest = (aid, dist)
        outcome = "hit" if nearest else "miss"
        if (not nearest and intended_tr is not None
                and intended_tr.at(first_time + t) is None):
            outcome = "untracked"
        return {
            "outcome": outcome,
            "aid": nearest[0] if nearest else None,
            "t_hit": t,
            "miss_m": nearest[1] if nearest else (best_miss if outcome == "miss" else None),
            "impact": impact,
        }

    def laser(self, shot):
        origin = shot["origin"]
        d = shot["dir"]
        st = shot["stats"]
        rng = float(st.get("range", 280.0))
        ts = shot["t"]
        step = 0.5
        s = 0.0
        targets = self.nearest_targets(ts)
        while s <= rng:
            p = (origin[0] + d[0] * s, origin[1] + d[1] * s, origin[2] + d[2] * s)
            if p[2] <= self.ground(p[0], p[1]):
                return {"outcome": "miss_ground", "aid": None, "t_hit": None,
                        "miss_m": None, "impact": p}
            for aid, tkind, tp in targets:
                if self.cylinder_hit(p, tp, tkind):
                    return {"outcome": "hit", "aid": aid, "t_hit": s / 252.0,
                            "miss_m": 0.0, "impact": p}
            s += step
        return {"outcome": "miss", "aid": None, "t_hit": None, "miss_m": None, "impact": None}

    def lightning(self, shot):
        st = shot["stats"]
        rng = float(st.get("range", 45.0))
        cone = math.radians(float(st.get("cone_deg", 30.0)))
        yaw = shot["aim"][0]
        best = None
        for aid, tkind, tp in self.nearest_targets(shot["t"]):
            dx, dy = tp[0] - shot["pos"][0], tp[1] - shot["pos"][1]
            dist = math.hypot(dx, dy)
            if dist > rng or dist < 0.1:
                continue
            if abs(ang_diff(math.atan2(dy, dx), yaw)) > cone:
                continue
            if best is None or dist < best[1]:
                best = (aid, dist)
        if best:
            return {"outcome": "hit", "aid": best[0], "t_hit": 0.0, "miss_m": 0.0,
                    "impact": None}
        return {"outcome": "miss", "aid": None, "t_hit": None, "miss_m": None, "impact": None}

    def evaluate(self, shot):
        kind = shot["kind"]
        if kind == "laser":
            return self.laser(shot)
        if kind == "lightning":
            return self.lightning(shot)
        return self.shell_or_bullet(shot, shot["t"])


# --------------------------------------------------------------------------- replay
class Replay:
    def __init__(self, msgs, stream_path, args):
        self.args = args
        self.hello = msgs[0]
        if self.hello.get("type") != "hello":
            raise SystemExit("replay: first stream message is not a hello")
        self.obs_list = [m for m in msgs if m.get("type") == "obs"]
        self.end = next((m for m in reversed(msgs) if m.get("type") == "end"), None)
        self.map_dir = resolve_map_dir(self.hello, stream_path, args.map_dir)
        hello = json.loads(json.dumps(self.hello))
        hello.setdefault("map", {})["dir"] = self.map_dir
        t0 = time.perf_counter()
        from main import Bot          # noqa: E402  (after sys.path setup)
        self.Bot = Bot
        self.bot = Bot(hello)
        self.ready_ms = (time.perf_counter() - t0) * 1000.0
        self.cfg = hello.get("config") or {}
        self.tl = Timeline(self.obs_list)
        self.hitsim = HitSim(self.bot.world, self.tl, self.cfg)

        self.sim = not args.no_sim
        self.arm = args.arm
        if self.sim:
            self.torso = Torso(self.obs_list[0]["self"]["aim"],
                               (self.obs_list[0]["self"].get("stats") or {}))
            self.weapons = SimWeapons(self.obs_list[0]["self"], self.cfg, self.arm)
            self.energy = float(self.obs_list[0]["self"].get("energy", 100.0))
        else:
            self.torso = None
            self.weapons = None
            self.energy = 100.0

        # metrics
        self.mode_decisions = Counter()
        self.mode_ticks = Counter()
        self.mode_switches = 0
        self.last_mode = None
        self.decisions = 0
        self.fails = 0
        self.distance = 0.0
        self.last_pos = None
        self.outside_zone = 0
        self.boost_decisions = 0
        self.jump_decisions = 0
        self.reload_decisions = 0
        self.fire_requests = Counter()
        self.shots = []
        self.shot_hits = Counter()
        self.shot_kinds = Counter()
        self.shot_misses = Counter()
        self.shot_untracked = Counter()
        self.aim_err_deg = {}
        self.aim_miss_m = {}
        self.hit_miss_m = {}
        self.target_dist = Dist()
        self.visible_dists = Dist()
        self.visible_decisions = 0
        self.target_decisions = 0
        self.events = []
        self.trace_lines = []
        self.trace_errors = 0
        self.untracked_decisions = 0
        self.combat_decisions = 0

    # -- one decision ----------------------------------------------------------------
    def run(self):
        max_obs = self.args.max_obs or len(self.obs_list)
        last_second = None
        for idx, obs in enumerate(self.obs_list[:max_obs]):
            t = float(obs["t"])
            me0 = obs["self"]
            if self.sim:
                self.torso.set_stats(me0.get("stats") or {})
                self2 = dict(me0)
                self2["aim"] = self.torso.aim()
                self2["weapons"] = self.weapons.public()
                self2["energy"] = self.energy
                obs2 = dict(obs)
                obs2["self"] = self2
            else:
                obs2 = obs
            # remember which target the combat module was asked about this decision
            info = {"target": None, "kind": None, "combat": False}
            combat = self.bot.combat

            def _decide(o, tr, now, _orig=combat.decide, _info=info):
                out = _orig(o, tr, now)
                _info["target"] = int(tr.id)
                _info["kind"] = combat.last_kind
                _info["combat"] = combat.last_kind is not None
                return out

            combat.decide = _decide
            try:
                act = self.bot.act(obs2)
            finally:
                del combat.decide
            if getattr(self.bot, "fails", 0) != self.fails:
                self.fails = self.bot.fails

            self.decisions += 1
            mode = getattr(self.bot.policy, "mode", "?")
            self.mode_decisions[mode] += 1
            self.mode_ticks[mode] += 2
            if self.last_mode is not None and mode != self.last_mode:
                self.mode_switches += 1
            self.last_mode = mode
            if info["combat"]:
                self.combat_decisions += 1

            pos = me0["pos"]
            if self.last_pos is not None:
                self.distance += math.hypot(pos[0] - self.last_pos[0], pos[1] - self.last_pos[1])
            self.last_pos = (pos[0], pos[1])
            if not me0.get("in_zone", True):
                self.outside_zone += 1
            visible = [m for m in obs.get("mechs") or [] if m.get("kind") == "mech"]
            if visible:
                self.visible_decisions += 1
                self.visible_dists.add(min(math.hypot(m["pos"][0] - pos[0],
                                                      m["pos"][1] - pos[1]) for m in visible))
            if info.get("target") is not None:
                self.target_decisions += 1
                trk = self.tl.get(info["target"])
                if trk is not None:
                    got = trk.at(t)
                    if got:
                        self.target_dist.add(math.hypot(got[0][0] - pos[0],
                                                        got[0][1] - pos[1]))

            move = act.get("move") or (0.0, 0.0)
            if act.get("boost"):
                self.boost_decisions += 1
            if act.get("jump"):
                self.jump_decisions += 1
            if act.get("reload"):
                self.reload_decisions += 1

            fire = list(act.get("fire") or [])
            if any(bool(x) for x in fire):
                kind = info["kind"] or (self.weapons.kinds()[0] if self.weapons else "?")
                self.fire_requests[kind] += 1

            self._record_events(obs)
            if self.args.trace:
                sec = int(t)
                if sec != last_second:
                    last_second = sec
                    self.trace_lines.append(self._trace_line(obs, mode, info, act))

            if not self.sim:
                # raw mode: aim error against the recorded actual aim at the decision
                if any(bool(x) for x in fire):
                    self._raw_aim_error(obs, info, act)
                continue

            # simulate the two server ticks this action applies to
            cmd = act.get("aim") or self.torso.aim()
            reload_req = bool(act.get("reload"))
            kind_of_slot = self.weapons.kinds()
            for k in (1, 2):
                tick_t = t + 0.05 * k
                self.torso.slew(cmd, 0.05)
                shots, spent = self.weapons.tick(
                    0.05, fire, reload_req, self.energy,
                    self.torso.aim(), pos, tick_t)
                self.energy = clamp(self.energy + 18.0 * 0.05 - spent, 0.0, 100.0)
                pos_at = self.tl.self_at(tick_t) or (pos[0], pos[1], pos[2])
                for sh in shots:
                    sh["t"] = tick_t
                    sh["pos"] = tuple(pos_at)
                    # origin must use the interpolated position, not the decision-time one
                    st = sh["stats"]
                    muz = float(st.get("muzzle_height", 7.0))
                    if sh["kind"] == "mortar":
                        muz += 2.5
                    d = sh["dir"]
                    sh["origin"] = (pos_at[0] + d[0] * 3.0, pos_at[1] + d[1] * 3.0,
                                    pos_at[2] + muz + d[2] * 3.0)
                    if info["combat"] and info["kind"] == sh["kind"]:
                        sh["target"] = info["target"]
                    elif info["combat"] and info["kind"] is None:
                        sh["target"] = None
                    else:
                        sh["target"] = info["target"] if info["kind"] == sh["kind"] else None
                    if not info["combat"]:
                        sh["crate"] = sh["kind"] == "gun"
                    self._record_shot(sh)

    # -- shot metrics ----------------------------------------------------------------
    def _record_shot(self, shot):
        res = self.hitsim.evaluate(shot)
        shot["result"] = res
        self.shots.append(shot)
        kind = shot["kind"]
        self.shot_kinds[kind] += 1
        if res["outcome"] == "hit":
            self.shot_hits[kind] += 1
        elif res["outcome"] == "untracked":
            self.shot_untracked[kind] += 1
        else:
            self.shot_misses[kind] += 1
            if res.get("miss_m") is not None:
                self.hit_miss_m.setdefault(kind, Dist()).add(res["miss_m"])
        self._aim_error_for_shot(shot)

    def _aim_error_for_shot(self, shot):
        if shot.get("target") is None:
            self.untracked_decisions += 1
            return
        tr = self.tl.get(shot["target"])
        if tr is None:
            self.untracked_decisions += 1
            return
        got = tr.at(shot["t"])
        if not got:
            self.untracked_decisions += 1
            return
        tp, tv = got
        me_pos = self.tl.self_at(shot["t"])
        if me_pos is None:
            return
        stats = self.tl.self_stats_at(shot["t"]) or {}
        trk = _TrackView(tr.kind, tp, tv, shot["t"])
        d = math.hypot(tp[0] - me_pos[0], tp[1] - me_pos[1])
        try:
            desired = self.bot.combat.aim_for(
                {"pos": list(me_pos), "stats": stats}, trk, shot["kind"], d, now=shot["t"])
        except TypeError:
            desired = self.bot.combat.aim_for(
                {"pos": list(me_pos), "stats": stats}, trk, shot["kind"], d)
        a = dir_from(shot["aim"][0], shot["aim"][1])
        b = dir_from(desired[0], desired[1])
        err = angle_between(a, b)
        self.aim_err_deg.setdefault(shot["kind"], Dist()).add(math.degrees(err))
        self.aim_miss_m.setdefault(shot["kind"], Dist()).add(d * math.sin(err))
        shot["aim_err_deg"] = math.degrees(err)
        shot["ideal"] = desired

    def _raw_aim_error(self, obs, info, act):
        me = obs["self"]
        if info.get("target") is None or not info.get("kind"):
            return
        tr = self.tl.get(info["target"])
        got = tr.at(obs["t"]) if tr else None
        if not got:
            return
        tp, tv = got
        trk = _TrackView(tr.kind, tp, tv, obs["t"])
        d = math.hypot(tp[0] - me["pos"][0], tp[1] - me["pos"][1])
        try:
            desired = self.bot.combat.aim_for(me, trk, info["kind"], d, now=obs["t"])
        except TypeError:
            desired = self.bot.combat.aim_for(me, trk, info["kind"], d)
        a = dir_from(me["aim"][0], me["aim"][1])
        b = dir_from(desired[0], desired[1])
        err = angle_between(a, b)
        self.aim_err_deg.setdefault(info["kind"], Dist()).add(math.degrees(err))
        self.aim_miss_m.setdefault(info["kind"], Dist()).add(d * math.sin(err))

    # -- events ----------------------------------------------------------------------
    def _record_events(self, obs):
        for ev in obs.get("events") or []:
            e = dict(ev)
            e["_t"] = float(obs["t"])
            e["_tick"] = obs.get("tick")
            self.events.append(e)

    def _trace_line(self, obs, mode, info, act):
        me = obs["self"]
        x, y, z = me["pos"]
        bits = ["t=%5.1f" % obs["t"], "%-6s" % mode,
                "pos=(%7.1f,%7.1f)" % (x, y), "hp=%4.0f" % me["hp"]]
        if self.weapons is not None:
            kinds = [k for k in self.weapons.kinds() if k]
            charge = 0.0
            ammo = 0
            for w in self.weapons.slots:
                if not w:
                    continue
                if w["kind"] == "gun":
                    ammo = int(w["ammo"])
                if w["kind"] == "laser":
                    charge = w["charge"]
            bits.append("gun_ammo=%d" % ammo)
            bits.append("charge=%.2f" % charge)
            bits.append("wpn=%s" % ",".join(kinds))
        if info.get("target") is not None:
            tr = self.tl.get(info["target"])
            got = tr.at(obs["t"]) if tr else None
            if got:
                d = math.hypot(got[0][0] - x, got[0][1] - y)
                bits.append("tgt=%d d=%.0f" % (info["target"], d))
        f = act.get("fire") or []
        if any(f):
            bits.append("fire=%s" % "".join("1" if v else "0" for v in f))
        return " ".join(bits)

    # -- output ----------------------------------------------------------------------
    def summary(self):
        dur = (self.obs_list[-1]["t"] - self.obs_list[0]["t"]) if self.obs_list else 0.0
        out = {
            "stream": self.args.stream,
            "map_dir": self.map_dir,
            "hello": {"slot": self.hello.get("slot"), "seed": self.hello.get("seed")},
            "obs": len(self.obs_list),
            "duration_s": dur,
            "ready_ms": self.ready_ms,
            "decisions": self.decisions,
            "fails": self.fails,
            "sim": self.sim,
            "arm": self.arm,
            "mode_decisions": dict(self.mode_decisions),
            "mode_ticks": dict(self.mode_ticks),
            "mode_switches": self.mode_switches,
            "distance_m": self.distance,
            "outside_zone_decisions": self.outside_zone,
            "boost_decisions": self.boost_decisions,
            "jump_decisions": self.jump_decisions,
            "reload_decisions": self.reload_decisions,
            "combat_decisions": self.combat_decisions,
            "target_decisions": self.target_decisions,
            "target_dist_m": self.target_dist.summary(),
            "visible_decisions": self.visible_decisions,
            "visible_dist_m": self.visible_dists.summary(),
            "fire_requests": dict(self.fire_requests),
            "shots": dict(self.shot_kinds),
            "hits": dict(self.shot_hits),
            "misses": dict(self.shot_misses),
            "untracked_shots": dict(self.shot_untracked),
            "shot_details": [
                {
                    "t": round(s["t"], 3), "kind": s["kind"], "target": s.get("target"),
                    "outcome": s["result"]["outcome"], "aid": s["result"].get("aid"),
                    "miss_m": s["result"].get("miss_m"),
                    "aim_err_deg": s.get("aim_err_deg"),
                    "charge": s.get("charge"), "crate": s.get("crate", False),
                }
                for s in self.shots
            ],
            "aim_err_deg": {k: d.summary() for k, d in self.aim_err_deg.items()},
            "aim_miss_m": {k: d.summary() for k, d in self.aim_miss_m.items()},
            "hit_miss_m": {k: d.summary() for k, d in self.hit_miss_m.items()},
            "untracked_decisions": self.untracked_decisions,
            "events": self.events,
            "end": self.end,
        }
        return out

    def print_summary(self):
        s = self.summary()
        print("=" * 78)
        print("replay  %s" % s["stream"])
        print("map     %s   obs %d   %.1f s   ready %.0f ms"
              % (s["map_dir"], s["obs"], s["duration_s"], s["ready_ms"]))
        print("bot     decisions %d   failures %d   simulated=%s%s"
              % (s["decisions"], s["fails"], s["sim"],
                 ("   arm=%s" % self.arm) if self.arm else ""))
        print("-" * 78)
        total = max(1, sum(self.mode_decisions.values()))
        print("policy modes (decision ticks / sim ticks):")
        for mode, n in self.mode_decisions.most_common():
            print("  %-8s %5d  %5.1f%%   (%d ticks)  switches=%d"
                  % (mode, n, 100.0 * n / total, n * 2, self.mode_switches))
        print("-" * 78)
        print("recorded self path: %.1f m" % s["distance_m"])
        print("outside zone: %d decisions (%d ticks)"
              % (s["outside_zone_decisions"], s["outside_zone_decisions"] * 2))
        print("boost: %d decisions (%d ticks)   jump: %d   reload: %d"
              % (s["boost_decisions"], s["boost_decisions"] * 2,
                 s["jump_decisions"], s["reload_decisions"]))
        if self.sim:
            print("weapons (simulated): %s" % ",".join(k for k in self.weapons.kinds() if k))
            print("-" * 78)
            print("combat target: %d decisions, dist %s"
                  % (s["target_decisions"], self.target_dist.fmt(" m")))
            print("enemy mech visible: %d decisions, nearest dist %s"
                  % (s["visible_decisions"], self.visible_dists.fmt(" m")))
            print("-" * 78)
            print("simulated shots / hits (untracked = target left the recording before impact):")
            for kind in ("gun", "laser", "mortar", "lightning"):
                n = self.shot_kinds.get(kind, 0)
                if not n:
                    continue
                h = self.shot_hits.get(kind, 0)
                misses = self.shot_misses.get(kind, 0)
                unt = self.shot_untracked.get(kind, 0)
                print("  %-9s shots %4d  hits %4d (%5.1f%%)  tracked misses %3d"
                      "  untracked %3d  miss dist %s"
                      % (kind, n, h, 100.0 * h / max(1, n - unt), misses, unt,
                         self.hit_miss_m.get(kind, Dist()).fmt(" m")))
            if self.shots:
                crates = sum(1 for sh in self.shots if sh.get("crate"))
                if crates:
                    print("  note: %d gun shot(s) had no recorded mech target (crate shots)" % crates)
            print("-" * 78)
            print("aim error at fire time (deg, actual torso aim vs ideal solution):")
            if not self.aim_err_deg:
                print("  no shot fired with a tracked target")
            for kind, dist in sorted(self.aim_err_deg.items()):
                print("  %-9s %s" % (kind, dist.fmt(" deg")))
            print("lateral miss implied by that aim error (m):")
            for kind, dist in sorted(self.aim_miss_m.items()):
                print("  %-9s %s" % (kind, dist.fmt(" m")))
            print("fire requests (decisions with any trigger): %s"
                  % (", ".join("%s %d" % kv for kv in sorted(self.fire_requests.items())) or "none"))
        print("-" * 78)
        print("events (%d):" % len(self.events))
        for e in self.events:
            print("  t=%5.1f %s" % (e["_t"], _format_event(e)))
        if self.end:
            print("-" * 78)
            print("end: %s" % json.dumps(self.end))
        print("=" * 78)


class _TrackView:
    """Minimal Track-shaped object handed to Combat.aim_for()."""

    __slots__ = ("kind", "x", "y", "z", "vx", "vy", "vz", "seen")

    def __init__(self, kind, pos, vel, seen):
        self.kind = kind
        self.x, self.y, self.z = pos
        self.vx, self.vy, self.vz = vel
        self.seen = seen


def _format_event(e):
    et = e.get("type")
    if et == "damaged":
        src = e.get("by")
        src = ("mech %s" % src) if src is not None else "zone"
        rear = " REAR" if e.get("rear") else ""
        return "DAMAGED %5.0f by %s with %s bearing=%s%s" % (
            float(e.get("amount") or 0), src, e.get("weapon"),
            "?" if e.get("bearing") is None else "%.2f" % e["bearing"], rear)
    if et == "hit":
        return "HIT     %5.0f on %s with %s%s" % (
            float(e.get("amount") or 0), e.get("target"), e.get("weapon"),
            " REAR" if e.get("rear") else "")
    if et == "kill":
        return "KILL    target %s" % e.get("target")
    if et == "effect":
        return "EFFECT  %s by %s" % (e.get("effect"), e.get("by"))
    if et == "take_failed":
        return "TAKE_FAILED %s (%s)" % (e.get("id"), e.get("reason"))
    if et == "dropped":
        return "DROPPED %s %s slot %s" % (e.get("id"), e.get("kind"), e.get("slot"))
    return "%s %s" % (et, json.dumps({k: v for k, v in e.items()
                                      if not k.startswith("_")})[:120])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stream", default=os.path.join(ROOT, "kit", "check", "stream.jsonl.gz"))
    ap.add_argument("--map-dir", default=None)
    ap.add_argument("--arm", default=None,
                    help="what-if second weapon: laser, mortar or lightning")
    ap.add_argument("--no-sim", action="store_true",
                    help="feed observations unchanged (raw recorded aim, no weapon/hit sim)")
    ap.add_argument("--trace", action="store_true", help="one line per match second")
    ap.add_argument("--max-obs", type=int, default=0, help="only replay the first N obs")
    ap.add_argument("--json", default=None, help="write the summary as JSON to this path")
    args = ap.parse_args(argv)

    if args.arm and args.arm not in ("laser", "mortar", "lightning"):
        raise SystemExit("replay: --arm must be laser, mortar or lightning")

    msgs = load_stream(args.stream)
    r = Replay(msgs, args.stream, args)
    r.run()
    r.print_summary()
    if args.trace:
        print("trace (one line per second):")
        for line in r.trace_lines:
            print("  " + line)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(r.summary(), f, indent=1)
        print("json -> %s" % args.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
