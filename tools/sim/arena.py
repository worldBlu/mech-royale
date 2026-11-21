"""Map geometry for the simulator.

Terrain heights, walkability and 2-D sight come from the bot's own baked ``World`` (the same
map the bot navigates) so the sim and the bot never disagree about where a wall is.  On top
of that we parse ``world.json`` prisms (buildings, stadium, bridges) into vertical intervals
so projectiles and beams can be blocked in 3-D: trees block sight only, never shots.

Simplifications (documented in docs/sim.md):
  * movement uses the bot's 2 m walk grid (no roof walking, no flying over buildings);
  * terrain occlusion for vision is the bot's 2-D sight grid (no canopy density nuance);
  * shots test exact prisms + a sampled terrain profile along the segment.
"""
import json
import math
import os

from . import kitcfg


class Arena:
    def __init__(self, map_dir=None):
        self.dir = os.path.abspath(map_dir or kitcfg.MAP_DIR)
        with open(os.path.join(self.dir, "v2", "world.json"), "rb") as f:
            w = json.load(f)
        b = w["bounds"]
        self.min_x = float(b["minX"])
        self.max_x = float(b["maxX"])
        self.min_y = float(b["minY"])
        self.max_y = float(b["maxY"])
        self.world = None
        # Prism spatial hash (8 m cells) for ray casts.
        self.gs = 8.0
        self.grid = {}
        self.prisms = []
        for p in w.get("prisms") or []:
            ring = p.get("ring") or []
            if len(ring) < 6:
                continue
            pr = {
                "kind": p.get("kind", ""),
                "base": float(p.get("base", -1e9)),
                "top": float(p.get("top", 1e9)),
                "ring": [float(v) for v in ring],
                "holes": [[float(v) for v in h] for h in (p.get("holes") or [])],
            }
            xs = ring[0::2]
            ys = ring[1::2]
            pr["bbox"] = (min(xs), min(ys), max(xs), max(ys))
            n = len(ring) // 2
            edges = []
            for k in range(n):
                x1 = ring[2 * k]
                y1 = ring[2 * k + 1]
                x2 = ring[2 * ((k + 1) % n)]
                y2 = ring[2 * ((k + 1) % n) + 1]
                edges.append((x1, y1, x2, y2))
            pr["edges"] = edges
            idx = len(self.prisms)
            self.prisms.append(pr)
            x0, y0, x1, y1 = pr["bbox"]
            i0 = int((x0 - self.min_x) / self.gs)
            i1 = int((x1 - self.min_x) / self.gs)
            j0 = int((y0 - self.min_y) / self.gs)
            j1 = int((y1 - self.min_y) / self.gs)
            for j in range(j0, j1 + 1):
                for i in range(i0, i1 + 1):
                    self.grid.setdefault((i, j), []).append(idx)
        self.tree_count = len((w.get("trees") or {}).get("data") or [])
        self._terrain = w["terrain"]

    def attach(self, world):
        self.world = world
        return self

    # ------------------------------------------------------------------ basics
    def ground(self, x, y):
        return self.world.terrain_z(x, y)

    def walkable(self, x, y):
        return self.world.walkable(x, y)

    def inside(self, x, y):
        return self.min_x <= x <= self.max_x and self.min_y <= y <= self.max_y

    # ------------------------------------------------------------- prism tests
    @staticmethod
    def _point_in_ring(x, y, ring):
        n = len(ring) >> 1
        inside = False
        x1 = ring[-2]
        y1 = ring[-1]
        for k in range(n):
            x2 = ring[2 * k]
            y2 = ring[2 * k + 1]
            if (y1 > y) != (y2 > y):
                if x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
                    inside = not inside
            x1, y1 = x2, y2
        return inside

    def _in_prism_xy(self, x, y, pr):
        if not self._point_in_ring(x, y, pr["ring"]):
            return False
        for h in pr["holes"]:
            if self._point_in_ring(x, y, h):
                return False
        return True

    def solid_point(self, x, y, z):
        """True when a point is inside a prism or below the terrain."""
        if z < self.ground(x, y) - 0.05:
            return True
        i = int((x - self.min_x) / self.gs)
        j = int((y - self.min_y) / self.gs)
        for idx in self.grid.get((i, j), ()):
            pr = self.prisms[idx]
            if not (pr["base"] <= z <= pr["top"]):
                continue
            x0, y0, x1, y1 = pr["bbox"]
            if x < x0 or x > x1 or y < y0 or y > y1:
                continue
            if self._in_prism_xy(x, y, pr):
                return True
        return False

    @staticmethod
    def _seg_2d_t(p0x, p0y, p1x, p1y, edge):
        """Parameter t in [0,1] where segment p0-p1 crosses edge (2-D), or None."""
        x3, y3, x4, y4 = edge
        dx = p1x - p0x
        dy = p1y - p0y
        ex = x4 - x3
        ey = y4 - y3
        den = dx * ey - dy * ex
        if abs(den) < 1e-12:
            return None
        qx = x3 - p0x
        qy = y3 - p0y
        t = (qx * ey - qy * ex) / den
        u = (qx * dy - qy * dx) / den
        if -1e-9 <= t <= 1.0 + 1e-9 and -1e-9 <= u <= 1.0 + 1e-9:
            return max(0.0, min(1.0, t))
        return None

    def _prism_hit(self, p0, p1, pr):
        z0 = p0[2]
        z1 = p1[2]
        base = pr["base"]
        top = pr["top"]
        if (z0 < base and z1 < base) or (z0 > top and z1 > top):
            return None
        x0, y0, x1, y1 = pr["bbox"]
        # Cheap bbox reject in 2-D.
        if max(p0[0], p1[0]) < x0 or min(p0[0], p1[0]) > x1:
            return None
        if max(p0[1], p1[1]) < y0 or min(p0[1], p1[1]) > y1:
            return None
        ts = [0.0, 1.0]
        for edge in pr["edges"]:
            t = self._seg_2d_t(p0[0], p0[1], p1[0], p1[1], edge)
            if t is not None:
                ts.append(t)
        for h in pr["holes"]:
            n = len(h) // 2
            for k in range(n):
                edge = (
                    h[2 * k],
                    h[2 * k + 1],
                    h[2 * ((k + 1) % n)],
                    h[2 * ((k + 1) % n) + 1],
                )
                t = self._seg_2d_t(p0[0], p0[1], p1[0], p1[1], edge)
                if t is not None:
                    ts.append(t)
        ts.sort()
        for a, b in zip(ts, ts[1:]):
            if b - a < 1e-9:
                continue
            tm = 0.5 * (a + b)
            mx = p0[0] + (p1[0] - p0[0]) * tm
            my = p0[1] + (p1[1] - p0[1]) * tm
            if not self._in_prism_xy(mx, my, pr):
                continue
            za = z0 + (z1 - z0) * a
            zb = z0 + (z1 - z0) * b
            if min(za, zb) <= top and max(za, zb) >= base:
                return tm
        return None

    # ------------------------------------------------------------------ rays
    def raycast(self, p0, p1):
        """First solid hit along segment p0->p1: ``(t, (x, y, z))`` or None.

        Solids: prisms (buildings/stadium/bridges) and terrain.  Trees are ignored.
        """
        dx = p1[0] - p0[0]
        dy = p1[1] - p0[1]
        dz = p1[2] - p0[2]
        dist = math.hypot(dx, dy)
        if dist < 1e-6 and abs(dz) < 1e-6:
            return None
        # --- prisms, via the spatial hash
        best_t = None
        if dist >= 0.2:
            cand = set()
            steps = int(dist / self.gs) + 1
            for k in range(steps + 1):
                t = k / steps
                x = p0[0] + dx * t
                y = p0[1] + dy * t
                key = (int((x - self.min_x) / self.gs), int((y - self.min_y) / self.gs))
                bucket = self.grid.get(key)
                if bucket:
                    cand.update(bucket)
            for idx in cand:
                t = self._prism_hit(p0, p1, self.prisms[idx])
                if t is not None and (best_t is None or t < best_t):
                    best_t = t
        # --- terrain, only up to the prism hit (no point testing what is hidden)
        limit = 1.0 if best_t is None else best_t
        n = max(2, int(dist * limit / 1.5) + 2)
        for k in range(1, n + 1):
            t = (k / n) * limit
            x = p0[0] + dx * t
            y = p0[1] + dy * t
            z = p0[2] + dz * t
            if z <= self.ground(x, y) + 0.02:
                if best_t is None or t < best_t:
                    best_t = t
                break
        if best_t is None:
            return None
        return best_t, (p0[0] + dx * best_t, p0[1] + dy * best_t, p0[2] + dz * best_t)

    def raycast_dist(self, p0, p1):
        hit = self.raycast(p0, p1)
        if hit is None:
            return None
        d = math.dist(p0, p1)
        return hit[0] * d, hit[1]

    # ------------------------------------------------------------------ entities
    @staticmethod
    def ray_cylinder(p0, d, cx, cy, z_base, radius, height):
        """Ray/vertical-cylinder intersection parameter t in [0,1], or None.

        ``d`` is the full segment vector (p1-p0), not a unit direction.
        """
        ox = p0[0] - cx
        oy = p0[1] - cy
        if ox * ox + oy * oy <= radius * radius and z_base <= p0[2] <= z_base + height:
            return 0.0
        a = d[0] * d[0] + d[1] * d[1]
        if a < 1e-12:
            # vertical segment: check inside the disc
            if ox * ox + oy * oy <= radius * radius:
                z0, z1 = p0[2], p0[2] + d[2]
                if max(z0, z1) >= z_base and min(z0, z1) <= z_base + height:
                    return 0.0
            return None
        b = 2.0 * (ox * d[0] + oy * d[1])
        c = ox * ox + oy * oy - radius * radius
        disc = b * b - 4.0 * a * c
        if disc < 0.0:
            return None
        sq = math.sqrt(disc)
        t0 = (-b - sq) / (2.0 * a)
        t1 = (-b + sq) / (2.0 * a)
        for t in (t0, t1):
            if t < 0.0 or t > 1.0:
                continue
            z = p0[2] + d[2] * t
            if z_base <= z <= z_base + height:
                return t
        return None
