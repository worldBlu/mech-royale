"""Static map model for mechfight/1.

`world.json` (prisms, terrain description) and `terrain.i16` are mounted read-only at
`hello.map.dir`. The map is identical in every match, so it is turned once, at startup, into
a 2 m grid: walkable cells, ground height, and cells that block sight. The navigator, the
perception and the combat code query that grid.

Standard library only. Startup cost is dominated by rasterising the prisms (a few hundred
thousand point-in-polygon tests); everything else is O(cells).
"""
import json
import math
import os
import sys
from array import array

CELL = 2.0                 # metres per grid cell
STEP_UP = 1.2              # the mech walks up ledges this tall
MECH_H = 9.0               # mech body height (before cube scaling)
EYE = 8.0                  # eye height above pos.z (before cube scaling)
SLOPE_TAN = math.tan(math.radians(50.0))

WALK_BLOCK = 1


class World:
    """Baked map: `walk` and `sight` are row-major grids of nx*ny cells."""

    def __init__(self, map_dir):
        self.dir = map_dir
        path = os.path.join(map_dir, "v2", "world.json")
        with open(path, "rb") as f:
            w = json.load(f)
        b = w["bounds"]
        self.min_x = float(b["minX"])
        self.min_y = float(b["minY"])
        self.max_x = float(b["maxX"])
        self.max_y = float(b["maxY"])
        self.cell = CELL
        self.nx = int(math.ceil((self.max_x - self.min_x) / CELL)) + 1
        self.ny = int(math.ceil((self.max_y - self.min_y) / CELL)) + 1
        self.walk = bytearray(self.nx * self.ny)
        self.sight = bytearray(self.nx * self.ny)
        self.height = array("f", bytes(4 * self.nx * self.ny))

        self._load_terrain(w["terrain"], map_dir)
        self._bake_terrain()
        self._bake_prisms(w.get("prisms") or [])
        self._bake_trees(w.get("trees") or {})

    # ---------------------------------------------------------------- terrain
    def _load_terrain(self, t, map_dir):
        self.t_nx = int(t["nx"])
        self.t_ny = int(t["ny"])
        self.t_min_x = float(t["minX"])
        self.t_min_y = float(t["minY"])
        self.t_step = float(t.get("step", 1.0))
        self.t_scale = float(t.get("scale", 0.01))
        name = t.get("file", "terrain.i16")
        path = os.path.join(map_dir, "v2", name)   # world.json lives in v2/
        if not os.path.isfile(path):
            path = os.path.join(map_dir, name)
        a = array("h")
        with open(path, "rb") as f:
            a.frombytes(f.read())
        if sys.byteorder != "little":
            a.byteswap()
        self.terrain = a

    def terrain_z(self, x, y):
        """Bilinear ground height at (x, y), clamped to the terrain grid."""
        fx = (x - self.t_min_x) / self.t_step
        fy = (y - self.t_min_y) / self.t_step
        if fx < 0.0:
            fx = 0.0
        elif fx > self.t_nx - 1.001:
            fx = self.t_nx - 1.001
        if fy < 0.0:
            fy = 0.0
        elif fy > self.t_ny - 1.001:
            fy = self.t_ny - 1.001
        i = int(fx)
        j = int(fy)
        u = fx - i
        v = fy - j
        t = self.terrain
        n = self.t_nx
        h00 = t[j * n + i]
        h10 = t[j * n + i + 1]
        h01 = t[(j + 1) * n + i]
        h11 = t[(j + 1) * n + i + 1]
        return (
            h00 * (1.0 - u) * (1.0 - v)
            + h10 * u * (1.0 - v)
            + h01 * (1.0 - u) * v
            + h11 * u * v
        ) * self.t_scale

    def _bake_terrain(self):
        nx, ny, cell = self.nx, self.ny, CELL
        minx, miny = self.min_x, self.min_y
        walk = self.walk
        height = self.height
        for j in range(ny):
            y = miny + (j + 0.5) * cell
            row = j * nx
            for i in range(nx):
                x = minx + (i + 0.5) * cell
                h = self.terrain_z(x, y)
                height[row + i] = h
                dx = (self.terrain_z(x + 1.0, y) - self.terrain_z(x - 1.0, y)) * 0.5
                dy = (self.terrain_z(x, y + 1.0) - self.terrain_z(x, y - 1.0)) * 0.5
                if dx * dx + dy * dy > SLOPE_TAN * SLOPE_TAN:
                    walk[row + i] = WALK_BLOCK

    # ---------------------------------------------------------------- prisms
    @staticmethod
    def _in_ring(x, y, ring):
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

    def _bake_prisms(self, prisms):
        nx, ny, cell = self.nx, self.ny, CELL
        minx, miny = self.min_x, self.min_y
        walk, sight, height = self.walk, self.sight, self.height
        for p in prisms:
            ring = p.get("ring") or []
            if len(ring) < 6:
                continue
            holes = p.get("holes") or []
            base = float(p.get("base", -1e9))
            top = float(p.get("top", 1e9))
            xs = ring[0::2]
            ys = ring[1::2]
            i0 = max(0, int((min(xs) - minx) / cell))
            i1 = min(nx - 1, int((max(xs) - minx) / cell) + 1)
            j0 = max(0, int((min(ys) - miny) / cell))
            j1 = min(ny - 1, int((max(ys) - miny) / cell) + 1)
            for j in range(j0, j1 + 1):
                y = miny + (j + 0.5) * cell
                row = j * nx
                for i in range(i0, i1 + 1):
                    x = minx + (i + 0.5) * cell
                    if not self._in_ring(x, y, ring):
                        continue
                    if holes and any(self._in_ring(x, y, h) for h in holes):
                        continue
                    k = row + i
                    h = height[k]
                    if base <= h + STEP_UP and top > h + STEP_UP:
                        walk[k] = WALK_BLOCK          # a wall at walking height
                    if base < h + EYE and top > h + 1.5:
                        sight[k] = 1                  # blocks the eye line

    def _bake_trees(self, trees):
        fields = trees.get("fields") or []
        data = trees.get("data") or []
        if not fields or not data:
            return
        try:
            kx = fields.index("x")
            ky = fields.index("y")
            ktr = fields.index("trunkR")
        except ValueError:
            return
        nf = len(fields)
        nx, ny, cell = self.nx, self.ny, CELL
        minx, miny = self.min_x, self.min_y
        sight = self.sight
        for n in range(0, len(data) - nf + 1, nf):
            x = data[n + kx]
            y = data[n + ky]
            r = data[n + ktr]
            if r <= 0.05:
                continue
            i0 = max(0, int((x - r - minx - cell * 0.5) / cell))
            i1 = min(nx - 1, int((x + r - minx + cell * 0.5) / cell) + 1)
            j0 = max(0, int((y - r - miny - cell * 0.5) / cell))
            j1 = min(ny - 1, int((y + r - miny + cell * 0.5) / cell) + 1)
            for j in range(j0, j1 + 1):
                cy = miny + (j + 0.5) * cell
                row = j * nx
                for i in range(i0, i1 + 1):
                    cx = minx + (i + 0.5) * cell
                    if (cx - x) * (cx - x) + (cy - y) * (cy - y) <= (r + cell * 0.35) ** 2:
                        sight[row + i] = 1            # trunks block sight, not shots

    # ---------------------------------------------------------------- queries
    def cell_of(self, x, y):
        i = int((x - self.min_x) / CELL)
        j = int((y - self.min_y) / CELL)
        if i < 0:
            i = 0
        elif i >= self.nx:
            i = self.nx - 1
        if j < 0:
            j = 0
        elif j >= self.ny:
            j = self.ny - 1
        return i, j

    def cell_xy(self, i, j):
        return self.min_x + (i + 0.5) * CELL, self.min_y + (j + 0.5) * CELL

    def walkable(self, x, y):
        i, j = self.cell_of(x, y)
        return not self.walk[j * self.nx + i]

    def walkable_cell(self, i, j):
        return not self.walk[j * self.nx + i]

    def inside(self, x, y):
        return self.min_x <= x <= self.max_x and self.min_y <= y <= self.max_y

    def height_at(self, x, y):
        i, j = self.cell_of(x, y)
        return self.height[j * self.nx + i]

    def sight_blocked_cell(self, i, j):
        return self.sight[j * self.nx + i]

    def has_los(self, x0, y0, x1, y1):
        """2D line of sight through the sight grid (buildings and tree trunks)."""
        dx = x1 - x0
        dy = y1 - y0
        dist = math.hypot(dx, dy)
        if dist < 0.5:
            return True
        steps = int(dist / (CELL * 0.5)) + 1
        sx = dx / steps
        sy = dy / steps
        x = x0
        y = y0
        nx, ny = self.nx, self.ny
        sight = self.sight
        minx, miny = self.min_x, self.min_y
        i = int((x - minx) / CELL)
        j = int((y - miny) / CELL)
        for _ in range(steps):
            x += sx
            y += sy
            i = int((x - minx) / CELL)
            j = int((y - miny) / CELL)
            if 0 <= i < nx and 0 <= j < ny and sight[j * nx + i]:
                return False
        return True

    def path_clear(self, x0, y0, x1, y1):
        """Can the mech walk straight from A to B on the walk grid?"""
        dx = x1 - x0
        dy = y1 - y0
        dist = math.hypot(dx, dy)
        if dist < 0.5:
            return True
        steps = int(dist / (CELL * 0.5)) + 1
        sx = dx / steps
        sy = dy / steps
        x = x0
        y = y0
        nx, ny = self.nx, self.ny
        walk = self.walk
        minx, miny = self.min_x, self.min_y
        for _ in range(steps):
            x += sx
            y += sy
            i = int((x - minx) / CELL)
            j = int((y - miny) / CELL)
            if i < 0 or j < 0 or i >= nx or j >= ny:
                return False
            if walk[j * nx + i]:
                return False
        return True

    def nearest_walkable(self, x, y, max_r=20.0):
        """Closest walkable cell centre to (x, y), or None."""
        if self.walkable(x, y):
            return x, y
        ci, cj = self.cell_of(x, y)
        best = None
        best_d = max_r * max_r
        rmax = int(max_r / CELL) + 1
        for r in range(1, rmax + 1):
            for dj in range(-r, r + 1):
                for di in range(-r, r + 1):
                    if max(abs(di), abs(dj)) != r:
                        continue
                    i = ci + di
                    j = cj + dj
                    if i < 0 or j < 0 or i >= self.nx or j >= self.ny:
                        continue
                    if self.walk[j * self.nx + i]:
                        continue
                    px, py = self.cell_xy(i, j)
                    d = (px - x) ** 2 + (py - y) ** 2
                    if d < best_d:
                        best_d = d
                        best = (px, py)
            if best is not None:
                return best
        return best
