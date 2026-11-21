"""Pathfinding and steering on the baked 2 m grid.

A* over the walk grid with an octile heuristic and a hard expansion cap, so one plan always fits
the 50 ms decision budget. A partial path (walk toward the closest cell reached) is fine because
the policy asks again a few times per second. Returned waypoints are smoothed with straight-line
walkability checks, and the goal itself is only appended when that line is verified clear, so a
returned path never crosses a blocked cell.

Two shortcuts keep the per-tick cost low: a straight line to the goal (42% of the goals the real
policy produced on this map) skips A* entirely, and a still-walkable suffix of the previous path
is reused when the goal moved by less than 4 m (the policy normally keeps the same goal while it
replans every 0.35 s). Unreachable goals are rejected up front from the baked component labels.

Only standard library. The scratch arrays are allocated once and reused with generation stamps,
so a plan allocates nothing per call except the small result list.
"""
import heapq
import math
from array import array

CELL = 2.0
SQRT2 = math.sqrt(2.0)
HEUR_W = 1.05              # slight greed: fewer expansions, a few percent longer paths
MAX_LOOKAHEAD = 24         # smoothing: furthest cell tried in one straight leg


class Navigator:
    def __init__(self, world, cfg=None, budget=1800):
        self.w = world
        self.path = []
        self.goal = None
        self.t_path = -1e9
        self.replan_s = 0.35
        self.goal_shift = 4.0
        nav = (cfg or {}).get("nav") or {}
        self.budget = max(64, int(nav.get("budget", budget)))
        self.replan_s = float(nav.get("replan_s", self.replan_s))
        self.use_los = bool(nav.get("los", True))
        self.use_reuse = bool(nav.get("reuse", True))
        self.last_expansions = 0
        self.last_truncated = False
        self.last_source = "none"

        # Scratch A* state on the border-padded walk grid (cell (i, j) -> (j+1)*stride + i+1).
        stride = getattr(world, "stride", 0)
        padded = getattr(world, "walkp", None)
        if not stride or padded is None:      # a World built before the bake existed
            stride = world.nx + 2
            padded = bytearray(b"\x01") * (stride * (world.ny + 2))
            for j in range(world.ny):
                src = j * world.nx
                dst = (j + 1) * stride + 1
                padded[dst:dst + world.nx] = world.walk[src:src + world.nx]
        self.stride = stride
        self.walkp = padded
        size = stride * (world.ny + 2)
        self._g = array("f", [0.0]) * size
        self._stamp = array("i", bytes(4 * size))
        self._closed = array("i", bytes(4 * size))
        self._parent = array("i", bytes(4 * size))
        self._gen = 0

    # ------------------------------------------------------------------ A*
    def _astar(self, si, sj, gi, gj):
        """Weighted A* between two walkable cells. Returns (cells, expansions, truncated)."""
        stride = self.stride
        walkp = self.walkp
        g = self._g
        stamp = self._stamp
        closed = self._closed
        parent = self._parent
        gen = self._gen + 1
        if gen > 0x3FFFFFFF:                  # generation counter wrapped: start over
            self._stamp = stamp = array("i", bytes(4 * len(stamp)))
            self._closed = closed = array("i", bytes(4 * len(closed)))
            gen = 1
        self._gen = gen
        start = (sj + 1) * stride + (si + 1)
        goal = (gj + 1) * stride + (gi + 1)
        g[start] = 0.0
        stamp[start] = gen
        parent[start] = -1
        gi1 = gi + 1
        gj1 = gj + 1
        dx = si - gi
        if dx < 0:
            dx = -dx
        dy = sj - gj
        if dy < 0:
            dy = -dy
        sq = SQRT2 - 2.0
        h0 = (dx + dy) + sq * (dx if dx < dy else dy)
        heap = [(h0 * HEUR_W, start)]
        push = heapq.heappush
        pop = heapq.heappop
        budget = self.budget
        expansions = 0
        best_h = h0
        best_k = start
        found = False
        while heap:
            _f, k = pop(heap)
            if closed[k] == gen:
                continue
            closed[k] = gen
            j = k // stride
            i = k - j * stride
            ddx = i - gi1
            if ddx < 0:
                ddx = -ddx
            ddy = j - gj1
            if ddy < 0:
                ddy = -ddy
            h = (ddx + ddy) + sq * (ddx if ddx < ddy else ddy)
            if h < best_h:
                best_h = h
                best_k = k
            if k == goal:
                best_k = goal
                found = True
                break
            expansions += 1
            if expansions >= budget:
                break
            gk = g[k]
            # ---- east
            nk = k + 1
            if not walkp[nk]:
                ng = gk + 1.0
                if closed[nk] != gen and (stamp[nk] != gen or ng < g[nk]):
                    ndx = ddx - 1 if ddx else 1
                    stamp[nk] = gen
                    g[nk] = ng
                    parent[nk] = k
                    push(heap, (ng + ((ndx + ddy) + sq * (ndx if ndx < ddy else ddy)) * HEUR_W, nk))
            # ---- west
            nk = k - 1
            if not walkp[nk]:
                ng = gk + 1.0
                if closed[nk] != gen and (stamp[nk] != gen or ng < g[nk]):
                    ndx = ddx + 1
                    stamp[nk] = gen
                    g[nk] = ng
                    parent[nk] = k
                    push(heap, (ng + ((ndx + ddy) + sq * (ndx if ndx < ddy else ddy)) * HEUR_W, nk))
            # ---- north (row + 1)
            nk = k + stride
            if not walkp[nk]:
                ng = gk + 1.0
                if closed[nk] != gen and (stamp[nk] != gen or ng < g[nk]):
                    ndy = ddy - 1 if ddy else 1
                    stamp[nk] = gen
                    g[nk] = ng
                    parent[nk] = k
                    push(heap, (ng + ((ddx + ndy) + sq * (ddx if ddx < ndy else ndy)) * HEUR_W, nk))
            # ---- south (row - 1)
            nk = k - stride
            if not walkp[nk]:
                ng = gk + 1.0
                if closed[nk] != gen and (stamp[nk] != gen or ng < g[nk]):
                    ndy = ddy + 1
                    stamp[nk] = gen
                    g[nk] = ng
                    parent[nk] = k
                    push(heap, (ng + ((ddx + ndy) + sq * (ddx if ddx < ndy else ndy)) * HEUR_W, nk))
            # ---- diagonals need both orthogonal cells free (no corner cutting)
            east_ok = not walkp[k + 1]
            west_ok = not walkp[k - 1]
            north_ok = not walkp[k + stride]
            south_ok = not walkp[k - stride]
            if east_ok and north_ok:
                nk = k + 1 + stride
                if not walkp[nk]:
                    ng = gk + SQRT2
                    if closed[nk] != gen and (stamp[nk] != gen or ng < g[nk]):
                        ndx = ddx - 1 if ddx else 1
                        ndy = ddy - 1 if ddy else 1
                        stamp[nk] = gen
                        g[nk] = ng
                        parent[nk] = k
                        push(heap, (ng + ((ndx + ndy) + sq * (ndx if ndx < ndy else ndy)) * HEUR_W, nk))
            if west_ok and north_ok:
                nk = k - 1 + stride
                if not walkp[nk]:
                    ng = gk + SQRT2
                    if closed[nk] != gen and (stamp[nk] != gen or ng < g[nk]):
                        ndx = ddx + 1
                        ndy = ddy - 1 if ddy else 1
                        stamp[nk] = gen
                        g[nk] = ng
                        parent[nk] = k
                        push(heap, (ng + ((ndx + ndy) + sq * (ndx if ndx < ndy else ndy)) * HEUR_W, nk))
            if east_ok and south_ok:
                nk = k + 1 - stride
                if not walkp[nk]:
                    ng = gk + SQRT2
                    if closed[nk] != gen and (stamp[nk] != gen or ng < g[nk]):
                        ndx = ddx - 1 if ddx else 1
                        ndy = ddy + 1
                        stamp[nk] = gen
                        g[nk] = ng
                        parent[nk] = k
                        push(heap, (ng + ((ndx + ndy) + sq * (ndx if ndx < ndy else ndy)) * HEUR_W, nk))
            if west_ok and south_ok:
                nk = k - 1 - stride
                if not walkp[nk]:
                    ng = gk + SQRT2
                    if closed[nk] != gen and (stamp[nk] != gen or ng < g[nk]):
                        ndx = ddx + 1
                        ndy = ddy + 1
                        stamp[nk] = gen
                        g[nk] = ng
                        parent[nk] = k
                        push(heap, (ng + ((ndx + ndy) + sq * (ndx if ndx < ndy else ndy)) * HEUR_W, nk))
        self.last_expansions = expansions
        self.last_truncated = not found
        return self._trace(best_k, start, stride), expansions, not found

    def _trace(self, k, start, stride):
        """Walk the parent chain back to the start cell and return cells in start..k order."""
        parent = self._parent
        cells = []
        while k != start:
            j = k // stride
            cells.append((k - j * stride - 1, j - 1))
            k = parent[k]
            if k < 0:                         # defensive: a corrupt chain must not hang
                break
        j = start // stride
        cells.append((start - j * stride - 1, j - 1))
        cells.reverse()
        return cells

    # ------------------------------------------------------------------ paths
    def _smooth(self, pts, start, goal):
        """Greedy straight-line shortcutting; every accepted leg is verified walkable."""
        out = []
        anchor = start
        i = 0
        n = len(pts)
        while i < n:
            j = min(n - 1, i + MAX_LOOKAHEAD)
            best = -1
            while j >= i:
                p = pts[j]
                if self.w.path_clear(anchor[0], anchor[1], p[0], p[1]):
                    best = j
                    break
                j -= 1
            if best < 0:                      # not even the next cell centre is reachable straight
                break
            out.append(pts[best])
            anchor = pts[best]
            i = best + 1
        # the real goal point (not a cell centre) is only appended when the line is clear
        if out and math.dist(anchor, goal) > 0.2:
            if self.w.path_clear(anchor[0], anchor[1], goal[0], goal[1]):
                out.append(goal)
        return out

    def _reuse(self, px, py, goal):
        """Suffix of the previous path, if the goal barely moved and the suffix still walks."""
        path = self.path
        if not path or self.goal is None:
            return None
        if math.dist(self.goal, goal) > self.goal_shift:
            return None
        n = len(path)
        j = 0
        while j < n and math.dist(path[j], (px, py)) < 2.2:
            j += 1                            # waypoints the mech already stands on
        if j >= n:
            return None
        best = -1
        stop = min(n, j + 3)
        k = j
        while k < stop:
            p = path[k]
            if self.w.path_clear(px, py, p[0], p[1]):
                best = k
                break
            k += 1
        if best < 0:
            return None
        out = list(path[best:])
        if math.dist(out[-1], goal) > 0.5:
            last = out[-1]
            if self.w.path_clear(last[0], last[1], goal[0], goal[1]):
                out.append(goal)
        return out

    def plan(self, pos, goal):
        """Waypoints from pos toward goal; empty when no verified route is available."""
        self.last_source = "none"
        self.last_expansions = 0
        self.last_truncated = False
        if goal is None:
            return []
        w = self.w
        gx, gy = goal
        if not w.inside(gx, gy):
            gx = min(max(gx, w.min_x + 2), w.max_x - 2)
            gy = min(max(gy, w.min_y + 2), w.max_y - 2)
            goal = (gx, gy)
        if not w.walkable(gx, gy):
            n = w.nearest_walkable(gx, gy)
            if n is not None:
                goal = n
        px, py = pos[0], pos[1]
        if math.dist((px, py), goal) < 2.0:
            self.last_source = "close"
            return [goal]
        if self.use_los and w.path_clear(px, py, goal[0], goal[1]):
            self.last_source = "los"
            return [goal]
        if self.use_reuse:
            out = self._reuse(px, py, goal)
            if out is not None:
                self.last_source = "reuse"
                return out

        nx = w.nx
        walk = w.walk
        si, sj = w.cell_of(px, py)
        if walk[sj * nx + si]:
            n = w.nearest_walkable(px, py)
            if n is None:
                return []
            si, sj = w.cell_of(*n)
        gi, gj = w.cell_of(goal[0], goal[1])
        if walk[gj * nx + gi]:
            n = w.nearest_walkable(goal[0], goal[1])
            if n is None:
                return []
            goal = n
            gi, gj = w.cell_of(*n)
        comp = getattr(w, "comp", None)
        if comp is not None and comp[sj * nx + si] != comp[gj * nx + gi]:
            self.last_source = "unreachable"
            return []
        if si == gi and sj == gj:
            self.last_source = "cell"
            return [goal]
        cells, _expansions, _truncated = self._astar(si, sj, gi, gj)
        self.last_source = "astar"
        if not cells:
            return []
        pts = [w.cell_xy(i, j) for i, j in cells]
        return self._smooth(pts, (px, py), goal)

    # ------------------------------------------------------------------ steering
    def steer(self, pos, goal, now, force=False):
        """Return (dx, dy, distance_left) for a unit-ish move, or None when there is no goal."""
        if goal is None:
            self.goal = None
            self.path = []
            return None
        if (
            force
            or now - self.t_path > self.replan_s
            or self.goal is None
            or math.dist(self.goal, goal) > self.goal_shift
            or not self.path
        ):
            self.path = self.plan(pos, goal)
            self.goal = goal
            self.t_path = now
        px, py = pos[0], pos[1]
        # drop waypoints we already stand on
        while self.path and math.dist(self.path[0], (px, py)) < 2.2:
            self.path.pop(0)
        if not self.path:
            # no verified route: aim straight at the goal for one replan interval
            dx = goal[0] - px
            dy = goal[1] - py
            d = math.hypot(dx, dy)
            if d < 1e-6:
                return (0.0, 0.0, 0.0)
            self.path = [goal]
            return (dx / d, dy / d, d)
        tx, ty = self.path[0]
        dx = tx - px
        dy = ty - py
        d = math.hypot(dx, dy)
        if d < 1e-6:
            return (0.0, 0.0, 0.0)
        return (dx / d, dy / d, math.dist(goal, (px, py)))
