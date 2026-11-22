"""Static map model for mechfight/1.

`world.json` (prisms, terrain description) and `terrain.i16` are mounted read-only at
`hello.map.dir`. The map is the same in every match, so the 2 m walk / sight / height grids are
baked offline by `tools/bake_map.py` into `bot/data/nav.bin` and loaded here at startup: a few
tens of milliseconds instead of the ~800 ms it takes to rasterise 259 prisms and 1560 trees.
A missing, stale (map hash mismatch) or corrupt bake file falls back to rasterising from
`world.json`, so `bot/` still runs on its own and the format can never make the bot fail.

Standard library only. `terrain_z` stays available on both paths (the replay tool samples it);
the baked height grid stores centimetres and `height_at` converts with `height_scale`.
"""
import json
import math
import os
import struct
import sys
import zlib
from array import array

CELL = 2.0                 # metres per grid cell
STEP_UP = 1.2              # the mech walks up ledges this tall
MECH_H = 9.0               # mech body height (before cube scaling)
EYE = 8.0                  # eye height above pos.z (before cube scaling)
SLOPE_TAN = math.tan(math.radians(50.0))

WALK_BLOCK = 1

# --- bake file (bot/data/nav.bin) ------------------------------------------------
# 72 byte little-endian header, then one zlib stream holding the four grids back to back:
#   walk[n]  sight[n]  height[n] int16 centimetres  comp[n] uint8 (255 = blocked)
# The header repeats every length so a truncated or foreign file is rejected instead of
# producing garbage grids.
BAKE_MAGIC = b"MECHNAV1"
BAKE_VERSION = 1
BAKE_HEADER = 72
_HDR = struct.Struct("<8sIIIfffffIIIIIIII")
# magic, format, header bytes, map hash, cell, min_x, min_y, max_x, max_y,
# nx, ny, payload bytes, walk bytes, sight bytes, height bytes, comp bytes, payload crc32

BAKE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "nav.bin")
BAKE_ENV = "MECH_NAV_BAKE"     # optional override, used by the bake/bench tools


def map_fingerprint(world_json_bytes, terrain_bytes):
    """CRC32 over world.json followed by terrain.i16, so a bake only loads on its own map."""
    c = zlib.crc32(world_json_bytes) & 0xFFFFFFFF
    return zlib.crc32(terrain_bytes, c) & 0xFFFFFFFF


def build_components(walk, nx, ny):
    """Label 4-connected walkable regions; 255 marks a cell the mech cannot stand on.

    Reachability for this navigator is exactly 4-connectivity: a diagonal step needs both
    orthogonal cells free (no corner cutting), and the reverse holds because any diagonal
    component is connected through its orthogonal cells too.
    """
    total = nx * ny
    comp = bytearray(b"\xff") * total
    label = 0
    for start in range(total):
        if walk[start] or comp[start] != 255:
            continue
        comp[start] = label
        stack = [start]
        while stack:
            k = stack.pop()
            j = k // nx
            i = k - j * nx
            if i > 0:
                n = k - 1
                if not walk[n] and comp[n] == 255:
                    comp[n] = label
                    stack.append(n)
            if i + 1 < nx:
                n = k + 1
                if not walk[n] and comp[n] == 255:
                    comp[n] = label
                    stack.append(n)
            if j > 0:
                n = k - nx
                if not walk[n] and comp[n] == 255:
                    comp[n] = label
                    stack.append(n)
            if j + 1 < ny:
                n = k + nx
                if not walk[n] and comp[n] == 255:
                    comp[n] = label
                    stack.append(n)
        if label < 254:
            label += 1
    return comp


def pack_bake(world):
    """Serialise a fully rasterised World (walk/sight/height/comp) as nav.bin bytes."""
    n = world.nx * world.ny
    if world.comp is None or len(world.comp) != n:
        raise ValueError("bake needs world.comp (build_components) for the whole grid")
    heights = array("h")
    if world.height_scale == 1.0:
        heights = array("h", (int(round(v * 100.0)) for v in world.height))
        if heights and (min(heights) < -32767 or max(heights) > 32767):
            raise ValueError("height grid does not fit int16 centimetres")
    else:
        heights = array("h", world.height)
    if heights.itemsize != 2:
        raise RuntimeError("array('h') is not 2 bytes on this platform")
    if sys.byteorder != "little":
        heights.byteswap()
    payload = bytes(world.walk) + bytes(world.sight) + heights.tobytes() + bytes(world.comp)
    data = zlib.compress(payload, 9)
    header = _HDR.pack(
        BAKE_MAGIC, BAKE_VERSION, BAKE_HEADER, world.map_hash, CELL,
        world.min_x, world.min_y, world.max_x, world.max_y,
        world.nx, world.ny, len(data), n, n, 2 * n, n, zlib.crc32(data),
    )
    return header + data


class World:
    """Baked map: `walk` and `sight` are row-major grids of nx*ny cells."""

    def __init__(self, map_dir, use_bake=True):
        self.dir = map_dir
        path = os.path.join(map_dir, "v2", "world.json")
        with open(path, "rb") as f:
            raw = f.read()
        w = json.loads(raw)
        b = w["bounds"]
        self.min_x = float(b["minX"])
        self.min_y = float(b["minY"])
        self.max_x = float(b["maxX"])
        self.max_y = float(b["maxY"])
        self.cell = CELL
        self.nx = int(math.ceil((self.max_x - self.min_x) / CELL)) + 1
        self.ny = int(math.ceil((self.max_y - self.min_y) / CELL)) + 1
        count = self.nx * self.ny

        terrain_bytes = self._load_terrain(w["terrain"], map_dir)
        self.map_hash = map_fingerprint(raw, terrain_bytes)

        self.baked = False
        self.comp = None
        if use_bake and self._load_bake(count):
            pass
        else:
            self.walk = bytearray(count)
            self.sight = bytearray(count)
            self.height = array("f", bytes(4 * count))
            self.height_scale = 1.0
            self._bake_terrain()
            self._bake_prisms(w.get("prisms") or [])
            self._bake_trees(w.get("trees") or {})
        self._build_padded()

    # ---------------------------------------------------------------- baked grids
    def _load_bake(self, count):
        """Load bot/data/nav.bin when it was baked for this exact map; False -> rasterise."""
        path = os.environ.get(BAKE_ENV) or BAKE_FILE
        try:
            with open(path, "rb") as f:
                blob = f.read()
        except OSError:
            return False
        if len(blob) < BAKE_HEADER:
            return False
        try:
            (magic, version, header, fingerprint, cell, min_x, min_y, max_x, max_y,
             nx, ny, payload_len, walk_len, sight_len, height_len, comp_len,
             crc) = _HDR.unpack_from(blob, 0)
        except struct.error:
            return False
        if (magic != BAKE_MAGIC or version != BAKE_VERSION or header != BAKE_HEADER
                or cell != CELL or nx != self.nx or ny != self.ny
                or fingerprint != self.map_hash):
            return False
        # bounds round-trip through f32 in the header: compare at millimetre tolerance
        if (abs(min_x - self.min_x) > 1e-3 or abs(min_y - self.min_y) > 1e-3
                or abs(max_x - self.max_x) > 1e-3 or abs(max_y - self.max_y) > 1e-3):
            return False
        if walk_len != count or sight_len != count or height_len != 2 * count or comp_len != count:
            return False
        if len(blob) != header + payload_len:
            return False
        payload = blob[header:]
        if (zlib.crc32(payload) & 0xFFFFFFFF) != crc:
            return False
        try:
            data = zlib.decompress(payload)
        except zlib.error:
            return False
        if len(data) != walk_len + sight_len + height_len + comp_len:
            return False
        self.walk = bytearray(data[:count])
        self.sight = bytearray(data[count:2 * count])
        heights = array("h")
        heights.frombytes(data[2 * count:4 * count])
        if sys.byteorder != "little":
            heights.byteswap()
        if heights.itemsize != 2 or len(heights) != count:
            return False
        self.height = heights
        self.height_scale = 0.01
        self.comp = bytearray(data[4 * count:])
        self.baked = True
        return True

    def _build_padded(self):
        """Border-padded walk grid used by the navigator (index (j+1)*stride + i+1)."""
        nx, ny = self.nx, self.ny
        stride = nx + 2
        padded = bytearray(b"\x01") * (stride * (ny + 2))
        walk = self.walk
        for j in range(ny):
            src = j * nx
            dst = (j + 1) * stride + 1
            padded[dst:dst + nx] = walk[src:src + nx]
        self.stride = stride
        self.walkp = padded

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
        with open(path, "rb") as f:
            raw = f.read()
        need = 2 * self.t_nx * self.t_ny
        if len(raw) < need:
            raise ValueError("terrain file %r is short: %d < %d bytes" % (path, len(raw), need))
        a = array("h")
        a.frombytes(raw[:need])
        if sys.byteorder != "little":
            a.byteswap()
        self.terrain = a
        return raw

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
        return self.height[j * self.nx + i] * self.height_scale

    def sight_blocked_cell(self, i, j):
        # Callers (cover scoring) probe neighbours of a candidate cell, so clamp instead of
        # trusting the index: an unclamped bytearray read killed the whole act() call.
        if i < 0:
            i = 0
        elif i >= self.nx:
            i = self.nx - 1
        if j < 0:
            j = 0
        elif j >= self.ny:
            j = self.ny - 1
        return self.sight[j * self.nx + i]

    def cover_quality(self, x, y):
        """0..1: fraction of the 8 surrounding 2 m cells whose sight line to (x, y) is blocked.

        Uses only the baked ``sight`` grid and walks short grid lines with no per-call
        allocation, so it is cheap enough for a steering decision every tick. Out-of-map
        neighbours count as blocked (the arena border is solid) and the point's own cell is
        never sampled, so standing inside a sight blocker does not by itself score as cover.
        """
        nx, ny = self.nx, self.ny
        sight = self.sight
        cell = self.cell
        minx, miny = self.min_x, self.min_y
        ci, cj = self.cell_of(x, y)
        blocked = 0
        for dj in (-1, 0, 1):
            for di in (-1, 0, 1):
                if di == 0 and dj == 0:
                    continue
                i = ci + di
                j = cj + dj
                if i < 0 or j < 0 or i >= nx or j >= ny:
                    blocked += 1             # solid arena border: it blocks sight too
                    continue
                if sight[j * nx + i]:
                    blocked += 1             # the neighbour cell itself is a sight blocker
                    continue
                sx = minx + (i + 0.5) * cell
                sy = miny + (j + 0.5) * cell
                dx = x - sx
                dy = y - sy
                steps = int(math.hypot(dx, dy) / (cell * 0.5)) + 1
                s = 1
                while s < steps:             # endpoint cell excluded: the target is not cover
                    t = s / steps
                    ii = int((sx + dx * t - minx) / cell)
                    jj = int((sy + dy * t - miny) / cell)
                    if 0 <= ii < nx and 0 <= jj < ny and sight[jj * nx + ii]:
                        blocked += 1
                        break
                    s += 1
        return blocked * 0.125

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
        """Can the mech walk straight from A to B on the walk grid?

        Exact cell traversal (DDA), not point sampling: every 2 m cell the segment passes
        through is checked, so a verified leg cannot clip a blocked cell at a corner between
        samples. The traversed cells of a sub-leg are a subset of the full leg's, so a
        direction verified from one position stays valid at every point on that line --
        which is what steering relies on between replans.
        """
        dx = x1 - x0
        dy = y1 - y0
        if dx * dx + dy * dy < 1e-9:
            return True
        nx = self.nx
        ny = self.ny
        walk = self.walk
        minx = self.min_x
        miny = self.min_y
        ux = (x0 - minx) / CELL
        uy = (y0 - miny) / CELL
        vx = (x1 - minx) / CELL
        vy = (y1 - miny) / CELL
        i = int(ux)
        j = int(uy)
        i1 = int(vx)
        j1 = int(vy)
        if i < 0 or j < 0 or i >= nx or j >= ny or i1 < 0 or j1 < 0 or i1 >= nx or j1 >= ny:
            return False
        if walk[j * nx + i] or walk[j1 * nx + i1]:
            return False
        if i == i1 and j == j1:
            return True
        dux = vx - ux
        duy = vy - uy
        if dux > 0.0:
            step_i = 1
            tx = (i + 1 - ux) / dux
            tdx = 1.0 / dux
        elif dux < 0.0:
            step_i = -1
            tx = (ux - i) / -dux
            tdx = -1.0 / dux
        else:
            step_i = 0
            tx = 2.0
            tdx = 0.0
        if duy > 0.0:
            step_j = 1
            ty = (j + 1 - uy) / duy
            tdy = 1.0 / duy
        elif duy < 0.0:
            step_j = -1
            ty = (uy - j) / -duy
            tdy = -1.0 / duy
        else:
            step_j = 0
            ty = 2.0
            tdy = 0.0
        for _ in range(abs(i1 - i) + abs(j1 - j)):       # bounded by the cell distance
            if tx < ty - 1e-7:
                i += step_i
                tx += tdx
            elif ty < tx - 1e-7:
                j += step_j
                ty += tdy
            else:
                # The segment passes exactly through a cell corner: it touches the two cells
                # beside the corner for a point, so treat that as crossing them. Otherwise a
                # corner-clipping line would be accepted here and rejected a few metres later
                # by an almost identical sub-segment (floating point decides which cells the
                # tiny diagonal entry lands in).
                ni = i + step_i
                nj = j + step_j
                if ni < 0 or ni >= nx or walk[j * nx + ni]:
                    return False
                if nj < 0 or nj >= ny or walk[nj * nx + i]:
                    return False
                i = ni
                j = nj
                tx += tdx
                ty += tdy
            if i < 0 or j < 0 or i >= nx or j >= ny:
                return False
            if walk[j * nx + i]:
                return False
            if i == i1 and j == j1:
                return True
        return False

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
