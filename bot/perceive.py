"""Tracks, hearing and threat bookkeeping.

The server only lists mechs it can see right now, but it remembers a mech for `memory_s`
after it leaves view. `Perception` keeps that memory itself as well (a superset: a track
we saw later than the server's memory window is still useful for guessing where someone
went), plus a crude hearing model: bearing is noisy, distance is a bucket.
"""
import math

NEAR = 60.0
MID = 160.0


class Track:
    __slots__ = (
        "id", "kind", "x", "y", "z", "vx", "vy", "vz", "hp", "max_hp",
        "weapons", "charging", "seen", "first_seen", "cubes", "hit_at",
    )

    def __init__(self, aid, kind, pos, vel, t):
        self.id = aid
        self.kind = kind
        self.x = float(pos[0])
        self.y = float(pos[1])
        self.z = float(pos[2])
        self.vx = float(vel[0])
        self.vy = float(vel[1])
        self.vz = float(vel[2])
        self.hp = None
        self.max_hp = None
        self.weapons = None
        self.charging = False
        self.seen = t
        self.first_seen = t
        self.cubes = 0
        self.hit_at = -1e9

    def update(self, a, t):
        self.x = float(a["pos"][0])
        self.y = float(a["pos"][1])
        self.z = float(a["pos"][2])
        self.vx = float(a["vel"][0])
        self.vy = float(a["vel"][1])
        self.vz = float(a["vel"][2])
        if "hp" in a:
            self.hp = a["hp"]
        if "max_hp" in a:
            self.max_hp = a["max_hp"]
        if "weapons" in a:
            self.weapons = a["weapons"]
        self.charging = bool(a.get("charging", False))
        self.cubes = a.get("cubes", self.cubes)
        self.seen = t

    def pos(self):
        return (self.x, self.y, self.z)

    def dist(self, p):
        return math.hypot(self.x - p[0], self.y - p[1])


class Perception:
    def __init__(self, world, cfg):
        self.w = world
        self.memory = float((cfg.get("vision") or {}).get("memory_s", 5.0))
        self.tracks = {}
        self.sounds = []          # (x, y, t, kind, age)
        self.last_damaged = -1e9
        self.last_attacker = None
        self.hp = None
        self._me = (0.0, 0.0)

    # ------------------------------------------------------------------ update
    def update(self, obs):
        t = obs["t"]
        me = obs["self"]
        mx, my, mz = me["pos"]
        self.hp = me["hp"]
        self._me = (mx, my)
        for a in obs.get("mechs") or []:
            aid = int(a["id"])
            tr = self.tracks.get(aid)
            if tr is None:
                self.tracks[aid] = Track(aid, a.get("kind", "mech"), a["pos"], a["vel"], t)
            else:
                tr.update(a, t)
        for ev in obs.get("events") or []:
            et = ev.get("type")
            if et == "damaged":
                self.last_damaged = t
                by = ev.get("by")
                if by is not None:
                    self.last_attacker = by
                    tr = self.tracks.get(int(by))
                    if tr is not None and tr.seen < t - 0.2:
                        br = ev.get("bearing")
                        if br is not None:
                            d = 60.0
                            tr.x = mx + math.cos(br) * d
                            tr.y = my + math.sin(br) * d
                            tr.seen = t
            elif et == "hit":
                tr = self.tracks.get(int(ev.get("target", -1)))
                if tr is not None:
                    tr.hit_at = t
                    amt = ev.get("amount") or 0.0
                    if tr.hp is not None:
                        tr.hp = max(1.0, tr.hp - amt)
            elif et == "kill":
                self.tracks.pop(int(ev.get("target", -1)), None)
            elif et == "dropped":
                pass
        # hearing: keep the last 3 s of bearings, with a bucket midpoint as distance
        for s in obs.get("sounds") or []:
            kind = s.get("kind", "?")
            br = s.get("bearing")
            if br is None:
                continue
            dist = s.get("dist", "far")
            d = NEAR * 0.5 if dist == "near" else (NEAR + (MID - NEAR) * 0.5 if dist == "mid" else MID + 40.0)
            age = float(s.get("age") or 0.0)
            self.sounds.append((mx + math.cos(br) * d, my + math.sin(br) * d, t - age, kind, age))
        if self.sounds:
            self.sounds = [s for s in self.sounds if t - s[2] < 3.0]

    def forget(self, t):
        dead = [k for k, tr in self.tracks.items() if t - tr.seen > self.memory + 0.5]
        for k in dead:
            del self.tracks[k]

    # ------------------------------------------------------------------ queries
    def hostiles(self, t, max_dist=250.0, fresh_only=False):
        me = self._me
        out = []
        for tr in self.tracks.values():
            if tr.kind != "mech":
                continue
            age = t - tr.seen
            if age > self.memory:
                continue
            if fresh_only and age > 0.35:
                continue
            d = tr.dist(me)
            if d <= max_dist:
                out.append((d, tr))
        out.sort(key=lambda p: p[0])
        return out

    def npcs(self, t, max_dist=120.0):
        me = self._me
        out = []
        for tr in self.tracks.values():
            if tr.kind != "npc":
                continue
            if t - tr.seen > 2.0:
                continue
            d = tr.dist(me)
            if d <= max_dist:
                out.append((d, tr))
        out.sort(key=lambda p: p[0])
        return out

    def visible(self, tr, mx, my, mz, eye):
        """Rough visibility test matching the server's eye-line rule."""
        if not self.w.has_los(mx, my, tr.x, tr.y):
            return False
        return True

    def danger_count(self, t, radius=110.0):
        return len(self.hostiles(t, radius, fresh_only=True))

    def freshest_sound(self, t):
        if not self.sounds:
            return None
        return max(self.sounds, key=lambda s: s[2])
