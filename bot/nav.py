"""Pathfinding and steering on the baked 2 m grid.

A* over the walk grid with an octile heuristic, capped so a single replan stays inside a
couple of milliseconds; a partial path is fine because the policy asks again every few
hundred milliseconds. Waypoints are smoothed with straight-line walkability checks.
"""
import heapq
import math

CELL = 2.0
SQRT2 = math.sqrt(2.0)

NEI = (
    (1, 0, 1.0),
    (-1, 0, 1.0),
    (0, 1, 1.0),
    (0, -1, 1.0),
    (1, 1, SQRT2),
    (1, -1, SQRT2),
    (-1, 1, SQRT2),
    (-1, -1, SQRT2),
)


class Navigator:
    def __init__(self, world, cfg=None, budget=4000):
        self.w = world
        self.budget = budget
        self.path = []
        self.goal = None
        self.t_path = -1e9
        self.replan_s = 0.35

    # ------------------------------------------------------------------ A*
    def _astar(self, sx, sy, gx, gy, budget=None):
        w = self.w
        nx = w.nx
        ny = w.ny
        walk = w.walk
        si, sj = w.cell_of(sx, sy)
        gi, gj = w.cell_of(gx, gy)
        if walk[sj * nx + si]:
            n = w.nearest_walkable(sx, sy)
            if n is None:
                return []
            si, sj = w.cell_of(*n)
        if walk[gj * nx + gi]:
            n = w.nearest_walkable(gx, gy)
            if n is None:
                return []
            gi, gj = w.cell_of(*n)
        if si == gi and sj == gj:
            return [(gx, gy)]

        budget = budget or self.budget
        open_heap = [(0.0, si, sj)]
        g = {(si, sj): 0.0}
        parent = {}
        closed = set()
        best = (1e18, si, sj)
        pushes = 0
        while open_heap:
            f, i, j = heapq.heappop(open_heap)
            key = (i, j)
            if key in closed:
                continue
            closed.add(key)
            gi_tmp = g.get(key, 1e18)
            dx = abs(i - gi)
            dy = abs(j - gj)
            h = (dx + dy) + (SQRT2 - 2.0) * min(dx, dy)
            if h < best[0]:
                best = (h, i, j)
            if i == gi and j == gj:
                return self._reconstruct(parent, gi, gj)
            if len(closed) > budget:
                break
            for di, dj, cost in NEI:
                ni = i + di
                nj = j + dj
                if ni < 0 or nj < 0 or ni >= nx or nj >= ny:
                    continue
                if walk[nj * nx + ni]:
                    continue
                if di and dj:
                    if walk[j * nx + ni] or walk[nj * nx + i]:
                        continue  # no corner cutting
                nk = (ni, nj)
                if nk in closed:
                    continue
                ng = gi_tmp + cost
                if ng >= g.get(nk, 1e18):
                    continue
                g[nk] = ng
                parent[nk] = key
                dx = abs(ni - gi)
                dy = abs(nj - gj)
                nh = (dx + dy) + (SQRT2 - 2.0) * min(dx, dy)
                heapq.heappush(open_heap, (ng + nh * 1.05, ni, nj))
                pushes += 1
        # out of budget: walk toward the closest cell we proved
        return self._reconstruct(parent, best[1], best[2])

    def _reconstruct(self, parent, i, j):
        cells = [(i, j)]
        while (i, j) in parent:
            i, j = parent[(i, j)]
            cells.append((i, j))
        cells.reverse()
        pts = [self.w.cell_xy(i, j) for i, j in cells]
        return pts

    def _smooth(self, pts, start):
        if len(pts) < 3:
            return pts
        out = []
        anchor = start
        i = 0
        n = len(pts)
        while i < n:
            # furthest point we can walk straight to from the anchor
            j = min(n - 1, i + 24)
            best = i
            while j > i:
                if self.w.path_clear(anchor[0], anchor[1], pts[j][0], pts[j][1]):
                    best = j
                    break
                j -= 1
            out.append(pts[best])
            anchor = pts[best]
            i = best + 1
        return out

    # ------------------------------------------------------------------ steering
    def plan(self, pos, goal):
        if goal is None:
            return []
        gx, gy = goal
        if not self.w.inside(gx, gy):
            gx = min(max(gx, self.w.min_x + 2), self.w.max_x - 2)
            gy = min(max(gy, self.w.min_y + 2), self.w.max_y - 2)
            goal = (gx, gy)
        if not self.w.walkable(gx, gy):
            n = self.w.nearest_walkable(gx, gy)
            if n is not None:
                goal = n
        if math.dist(pos[:2], goal) < 2.0:
            return [goal]
        raw = self._astar(pos[0], pos[1], goal[0], goal[1])
        if not raw:
            return [goal]
        raw[-1] = goal
        sm = self._smooth(raw, (pos[0], pos[1]))
        return sm

    def steer(self, pos, goal, now, force=False):
        """Return (dx, dy, distance_left) for a unit-ish move, or None when there is no goal."""
        if goal is None:
            return None
        if (
            force
            or now - self.t_path > self.replan_s
            or self.goal is None
            or math.dist(self.goal, goal) > 4.0
            or not self.path
        ):
            self.path = self.plan(pos, goal)
            self.goal = goal
            self.t_path = now
        if not self.path:
            return None
        px, py = pos[0], pos[1]
        # drop waypoints we already stand on
        while self.path and math.dist(self.path[0], (px, py)) < 2.2:
            self.path.pop(0)
        if not self.path:
            self.path = [goal]
        tx, ty = self.path[0]
        dx = tx - px
        dy = ty - py
        d = math.hypot(dx, dy)
        if d < 1e-6:
            return (0.0, 0.0, 0.0)
        # nudge around a wall corner that is very close
        return (dx / d, dy / d, math.dist(goal, (px, py)))
