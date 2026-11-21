"""Zone timeline: current circle, next circle, damage per second.

Protocol facts (kit/protocol.md 407-422): the circle holds, then shrinks linearly to the next
circle during ``shrink_s``; the next circle is entirely inside the current one; every phase's
damage rate applies during both its hold and its shrink.  The kit's recorded obs confirms the
absolute ``shrink_start`` / ``shrink_end`` times.

Phase timeline from the kit config (hold, shrink, radius):
    60, 50, 380 | 45, 40, 240 | 35, 35, 140 | 30, 30, 70 | 25, 25, 25 | 15, 25, 0
so the last circle closes at t=415 s and the match ends at 480 s.
"""
import math


class Zone:
    def __init__(self, cfg, rng):
        z = cfg["zone"]
        self.start_center = [float(z["start_center"][0]), float(z["start_center"][1])]
        self.start_radius = float(z["start_radius"])
        self.cfg = z
        self.phases = []
        cur_c = list(self.start_center)
        cur_r = self.start_radius
        t = 0.0
        for ph in z["phases"]:
            target_r = float(ph["radius"])
            # Uniform-area sample of a centre so the next circle fits inside the current
            # one: distance <= cur_r - target_r.  This mirrors "next circle entirely
            # inside current" and gives the same statistics as the server's "not
            # concentric" note.
            room = max(0.0, cur_r - target_r)
            d = math.sqrt(rng.random()) * room
            a = rng.random() * 2.0 * math.pi
            nc = [cur_c[0] + math.cos(a) * d, cur_c[1] + math.sin(a) * d]
            hold = float(ph["hold_s"])
            shrink = float(ph["shrink_s"])
            self.phases.append(
                {
                    "from_c": list(cur_c),
                    "from_r": cur_r,
                    "to_c": nc,
                    "to_r": target_r,
                    "hold_s": hold,
                    "shrink_s": shrink,
                    "dps": float(ph["dps"]),
                    "shrink_start": t + hold,
                    "shrink_end": t + hold + shrink,
                }
            )
            t += hold + shrink
            cur_c, cur_r = nc, target_r
        self.close_t = t
        self.center = list(self.start_center)
        self.radius = self.start_radius
        self.dps = float(self.phases[0]["dps"]) if self.phases else 0.0
        self.next_center = list(self.phases[0]["to_c"]) if self.phases else list(self.center)
        self.next_radius = float(self.phases[0]["to_r"]) if self.phases else 0.0
        self.shrink_start = float(self.phases[0]["shrink_start"]) if self.phases else 0.0
        self.shrink_end = float(self.phases[0]["shrink_end"]) if self.phases else 0.0
        self.phase_index = 0

    # ------------------------------------------------------------------ update
    def update(self, t):
        """Set the current/next state at match time ``t`` (seconds)."""
        phases = self.phases
        if not phases:
            return self.state()
        last = phases[-1]
        if t >= last["shrink_end"]:
            # closed; the last circle's damage rate keeps applying
            self.center = list(last["to_c"])
            self.radius = float(last["to_r"])
            self.dps = float(last["dps"])
            self.next_center = list(self.center)
            self.next_radius = float(self.radius)
            self.shrink_start = float(last["shrink_end"])
            self.shrink_end = float(last["shrink_end"])
            self.phase_index = len(phases) - 1
            return self.state()
        for idx, p in enumerate(phases):
            if t < p["shrink_start"]:
                self.center = list(p["from_c"])
                self.radius = float(p["from_r"])
                self.next_center = list(p["to_c"])
                self.next_radius = float(p["to_r"])
                self.shrink_start = float(p["shrink_start"])
                self.shrink_end = float(p["shrink_end"])
                self.dps = float(p["dps"])
                self.phase_index = idx
                return self.state()
            if t < p["shrink_end"]:
                u = (t - p["shrink_start"]) / max(1e-9, p["shrink_end"] - p["shrink_start"])
                self.center = [
                    p["from_c"][0] + (p["to_c"][0] - p["from_c"][0]) * u,
                    p["from_c"][1] + (p["to_c"][1] - p["from_c"][1]) * u,
                ]
                self.radius = p["from_r"] + (p["to_r"] - p["from_r"]) * u
                self.next_center = list(p["to_c"])
                self.next_radius = float(p["to_r"])
                self.shrink_start = float(p["shrink_start"])
                self.shrink_end = float(p["shrink_end"])
                self.dps = float(p["dps"])
                self.phase_index = idx
                return self.state()
        # only reachable with t < last.shrink_end handled above
        return self.state()

    def state(self):
        return {
            "center": [round(self.center[0], 2), round(self.center[1], 2)],
            "radius": round(self.radius, 2),
            "next_center": [round(self.next_center[0], 2), round(self.next_center[1], 2)],
            "next_radius": round(self.next_radius, 2),
            "shrink_start": round(self.shrink_start, 2),
            "shrink_end": round(self.shrink_end, 2),
            "dps": round(self.dps, 2),
        }

    # ------------------------------------------------------------------ queries
    def dist(self, x, y):
        return math.hypot(x - self.center[0], y - self.center[1])

    def in_zone(self, x, y, margin=0.0):
        return self.dist(x, y) <= self.radius + margin

    def dist_to_next(self, x, y):
        return math.hypot(x - self.next_center[0], y - self.next_center[1])
