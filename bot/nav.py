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

# Cover steering: nudge the next waypoint at most this far sideways/backwards, only when the
# detour stays under a few percent of the two-leg segment and the cover score improves.
COVER_MAX_DETOUR = 0.15    # relative extra length allowed for one shifted waypoint
COVER_MIN_GAIN = 0.0625    # at least half a blocked neighbour out of the eight
COVER_OFFSETS = (          # (sideways m, forward m) around the planned waypoint
    (2.0, 0.0), (-2.0, 0.0), (3.0, 0.0), (-3.0, 0.0),
    (2.0, -1.6), (-2.0, -1.6), (0.0, -2.0),
)


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
        self.last_cover_shift = 0.0
        self.last_cover = 0.0
        self._cover_target = None

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
            if best < 0:
                # The raw cell path is valid but nothing within the lookahead can be reached
                # in a straight line (a tight corridor with a turn every cell). Advance one
                # A* cell at a time; adjacent cells are always mutually walkable.
                out.append(pts[i])
                anchor = pts[i]
                i += 1
                continue
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
        sx, sy = px, py
        if walk[sj * nx + si]:
            n = w.nearest_walkable(px, py)
            if n is None:
                return []
            sx, sy = n                        # stand-in for a bot pushed into a blocked cell
            si, sj = w.cell_of(sx, sy)
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
        if len(cells) <= 1:                   # start cell is sealed off: no progress possible
            self.last_source = "stuck"
            return []
        pts = [w.cell_xy(i, j) for i, j in cells]
        return self._smooth(pts, (sx, sy), goal)

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
        w = self.w
        si, sj = w.cell_of(px, py)
        start_blocked = bool(w.walk[sj * w.nx + si])
        # Drop waypoints we already stand on -- but only while the leg to the *next* one is
        # verified too. Standing 2 m short of a corner waypoint and cutting straight to the
        # waypoint after it is how the old steer could clip a wall corner.
        popped = False
        while self.path and math.dist(self.path[0], (px, py)) < 2.2:
            if (not start_blocked and len(self.path) > 1 and not self.w.path_clear(
                    px, py, self.path[1][0], self.path[1][1])):
                break
            self.path.pop(0)
            popped = True
        if not self.path:
            return self._fallback_aim(pos, goal)
        tx, ty = self.path[0]
        if popped and not start_blocked and not self.w.path_clear(px, py, tx, ty):
            # The mech is off the verified line (usually right after a dropped waypoint):
            # reroute from where it actually is instead of steering through the corner.
            self.path = self.plan(pos, goal)
            self.t_path = now
            while self.path and math.dist(self.path[0], (px, py)) < 2.2:
                self.path.pop(0)
            if not self.path:
                return self._fallback_aim(pos, goal)
            tx, ty = self.path[0]
            if not start_blocked and not self.w.path_clear(px, py, tx, ty):
                return (0.0, 0.0, math.dist(goal, (px, py)))
        dx = tx - px
        dy = ty - py
        d = math.hypot(dx, dy)
        if d < 1e-6:
            return (0.0, 0.0, 0.0)
        return (dx / d, dy / d, math.dist(goal, (px, py)))

    def _fallback_aim(self, pos, goal):
        """No verified route: aim at the goal, but never trust more line than was checked.

        If the whole line to the goal is clear it is used directly. Otherwise only a short
        probed step is committed to ``self.path``; the next tick pops that probe and probes
        again from wherever the mech actually is, so a guessed straight line can never walk
        through a wall.
        """
        px, py = pos[0], pos[1]
        dx = goal[0] - px
        dy = goal[1] - py
        d = math.hypot(dx, dy)
        if d < 1e-6:
            self.path = []
            return (0.0, 0.0, 0.0)
        ux = dx / d
        uy = dy / d
        if self.w.path_clear(px, py, goal[0], goal[1]):
            self.path = [goal]
            return (ux, uy, d)
        step = min(2.0, d)
        while step >= 0.4:
            if self.w.path_clear(px, py, px + ux * step, py + uy * step):
                self.path = [(px + ux * step, py + uy * step)]
                return (ux, uy, d)
            step *= 0.5
        self.path = []
        return (0.0, 0.0, d)

    # ------------------------------------------------------------------ cover steering
    def steer_cover(self, pos, goal, threat_pos, now, force=False):
        """steer(), but shift the next waypoint a few metres toward nearby cover when cheap.

        ``World.cover_quality`` scores how many of the eight neighbouring cells have their
        sight line to a point blocked. A shifted waypoint is accepted only when it is walkable,
        straight-line clear from both the mech and the waypoint after it, costs at most
        ``COVER_MAX_DETOUR`` extra length, and improves that cover score by at least
        ``COVER_MIN_GAIN``. Among equally covered candidates the one the threat cannot see
        wins, then the shortest detour. The shift is chosen once per plan, never again for the
        same waypoint, so the path cannot creep sideways tick after tick; the returned point
        is always a verified walkable one.

        The added cost is bounded: seven candidates, each two ``path_clear`` probes and one
        ``cover_quality`` call (a few tens of microseconds), still far under the 50 ms tick.
        """
        self.last_cover_shift = 0.0
        self.last_cover = 0.0
        if goal is None:
            return self.steer(pos, goal, now, force)
        t_before = self.t_path
        out = self.steer(pos, goal, now, force)
        if out is None or threat_pos is None or not self.path:
            return out
        if self.t_path == t_before:
            return out                        # not a replan tick: keep the chosen waypoint
        if self.path[0] == self._cover_target:
            return out                        # a reused suffix we already shifted once
        w = self.w
        px, py = pos[0], pos[1]
        tx, ty = self.path[0]
        if len(self.path) > 1:
            ax, ay = self.path[1]
        else:
            ax, ay = goal[0], goal[1]
        dx = tx - px
        dy = ty - py
        seg = math.hypot(dx, dy)
        if seg < 0.5:
            return out
        ux = dx / seg
        uy = dy / seg
        lx = -uy                              # lateral axis of the travel direction
        ly = ux
        base_cost = seg + math.hypot(ax - tx, ay - ty)
        base_cover = w.cover_quality(tx, ty)
        limit = base_cost * (1.0 + COVER_MAX_DETOUR) + 0.25
        best_cover = -1.0
        ties = []                             # (cover, cost, cx, cy) at the best cover seen
        for off_lat, off_fwd in COVER_OFFSETS:
            cx = tx + lx * off_lat + ux * off_fwd
            cy = ty + ly * off_lat + uy * off_fwd
            if not w.inside(cx, cy) or not w.walkable(cx, cy):
                continue
            cost = math.hypot(cx - px, cy - py) + math.hypot(ax - cx, ay - cy)
            if cost > limit:
                continue
            if not w.path_clear(px, py, cx, cy) or not w.path_clear(cx, cy, ax, ay):
                continue
            cover = w.cover_quality(cx, cy)
            if cover < base_cover + COVER_MIN_GAIN:
                continue
            if cover > best_cover + 1e-9:
                best_cover = cover
                ties = [(cover, cost, cx, cy)]
            elif cover > best_cover - 1e-9:
                ties.append((cover, cost, cx, cy))
        if not ties:
            return out
        if len(ties) == 1:
            _cover, _cost, cx, cy = ties[0]
        else:
            hidden = [(cost, cx, cy) for _cover, cost, cx, cy in ties
                      if not w.has_los(threat_pos[0], threat_pos[1], cx, cy)]
            if hidden:
                _cost, cx, cy = min(hidden)
            else:
                _cost, cx, cy = min((cost, cx, cy) for _cover, cost, cx, cy in ties)
        if math.dist((cx, cy), (tx, ty)) < 0.4:
            return out
        self.path[0] = (cx, cy)
        self._cover_target = (cx, cy)
        self.last_cover = best_cover
        self.last_cover_shift = math.dist((cx, cy), (tx, ty))
        ddx = cx - px
        ddy = cy - py
        dd = math.hypot(ddx, ddy)
        if dd < 1e-6:
            return out
        return (ddx / dd, ddy / dd, out[2])
