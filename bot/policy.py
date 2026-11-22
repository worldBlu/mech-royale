"""Behaviour: zone discipline, looting, fighting and escaping.

Ranking only cares about surviving to the end, so the policy spends the early match on
power cubes and a second weapon, keeps itself inside the shrinking circle, and avoids
fights it cannot win. Movement goals come from here; the navigator turns them into a
steering direction and the combat module turns a target into aim and trigger.
"""
import json
import math
import os
import random

KIND_VALUE = {"laser": 3.0, "mortar": 2.0, "lightning": 2.2, "gun": 0.2}

# Tunables. The evaluation harness mutates this dict in place, so every knob below is read
# through ``self.k`` at decision time and an override takes effect on the next tick.
KNOBS = {
    "engage_dist": 260.0,       # do not pick a fight with a target farther than this
    "engage_hp_frac": 0.30,     # do not pick a fight with our own hp below this
    "engage_target_hp": 0.85,   # healthy target: only commit when we are clearly healthier
    "engage_our_hp": 0.72,
    "evade_hp_frac": 0.45,      # running away when a fresh hostile is close
    "evade_dist": 180.0,
    "evade_aimed_hp": 0.70,     # someone is pointing a weapon at us
    "evade_aimed_dist": 230.0,
    "evade_step": 90.0,
    "farm_threat": 260.0,
    "crate_threat": 260.0,
    "loot_threat": 60.0,
    "boost_energy": 22.0,
    "cover": 1,                 # rotate toward scenery that blocks lines of sight
    "heal": 1,                  # break contact and let the 45/s regen top us up
    "heal_hp": 0.95,            # start healing below this fraction
    "heal_exit": 0.97,          # stop at this fraction
    "jink": 1,                   # weave while moving so their gun has to lead a wobbly target
    "jink_frac": 0.55,
    "jink_hz": 1.1,
    "jink_dist": 260.0,
    "panic_dmg": 1e9,          # armor lost per second that makes us break contact
    "panic_s": 2.5,              # how long that duck-out lasts
    "heal_safe_dist": 250.0,    # only heal when the nearest fresh hostile is farther
    "cube_chase": 1,            # detour to power cubes lying around
    "target_weak": 1,           # prefer the most damaged fresh target over the nearest one
    "target_ttk": 0,            # choose the target we can finish soonest instead (experimental)
    "drop_early": 0,            # start walking to a supply drop before it lands
    "early_evade_s": 45.0,       # treat every fresh contact as a reason to run this early
    "dodge": 1,                 # side-step shells that are about to pass through us
    "rush_s": 90.0,              # opening weapon rush (laser/lightning) until this time
    "rush_dist": 170.0,
    "weapon_r": 150.0,          # weapons worth a detour even while a fight is near
    "weapon_threat": 25.0,      # never turn our back on a hostile this close to grab one         # only chase gear this far
    "finish_hp": 0.90,           # close in and finish targets below this hp fraction
    "finish_d": 35.0,           # preferred range while finishing
    "camp": 0,                  # hold a low-exposure spot inside the next zone
    "camp_frac": 0.75,          # stay inside this fraction of the next radius
    "camp_leave": 12.0,         # seconds of slack before the next shrink to relocate
    "outnumber": 0,             # extra caution when more than one fresh hostile is visible
    "evade_sticky": 3.0,        # keep running this long after the trigger clears
    "trade_hp": 0.80,           # disengage when we are losing a duel...
    "trade_dur": 2.5,           # ...for at least this many seconds
    "trade_target_hp": 0.92,    # only if the target is still this healthy
    "standoff": {"gun": 190.0, "laser": 220.0, "mortar": 150.0, "lightning": 28.0},
}


def _ang_diff(a, b):
    return (a - b + math.pi) % (2.0 * math.pi) - math.pi


def _knobs_from_env():
    """Per-process override, e.g. ``MF_POLICY_KNOBS={"engage_dist":120}``; used for A/B runs."""
    k = dict(KNOBS)
    raw = os.environ.get("MF_POLICY_KNOBS")
    if raw:
        try:
            k.update(json.loads(raw))
        except (ValueError, TypeError):
            pass
    return k


class Policy:
    def __init__(self, world, nav, perc, combat, cfg, seed=0):
        self.w = world
        self.nav = nav
        self.perc = perc
        self.combat = combat
        self.cfg = cfg
        self.rng = random.Random(seed)
        self.k = _knobs_from_env()
        self.mode = "LOOT"
        self.mode_t = -99.0
        self.goal = None
        self.take_id = None
        self.take_until = -1.0
        self.strafe_dir = 1.0
        self.hp_hist = []
        self.panic_until = 0.0
        self.strafe_t = -99.0
        self.last_pos = None
        self.stuck_t = -99.0
        self.stuck_n = 0
        self.jump_until = -99.0
        self.pref = 0.0
        self.evade_until = -99.0
        self.evade_from = None
        loot = cfg.get("loot") or {}
        cubes = cfg.get("cubes") or {}
        self.pickup_r = float(loot.get("pickup_radius", 4.0))
        self.cube_r = float(cubes.get("pickup_radius", 3.5))
        self.drops_cfg = cfg.get("drops") or {}

    # ------------------------------------------------------------------ main
    def act(self, obs):
        t = obs["t"]
        me = obs["self"]
        pos = (float(me["pos"][0]), float(me["pos"][1]), float(me["pos"][2]))
        self.perc.update(obs)
        self.perc.forget(t)
        act = {}
        offer = obs.get("offer")
        if offer:
            act["pick"] = self._pick_upgrade(offer, me)

        hp = float(me["hp"])
        max_hp = max(1.0, float(me["max_hp"]))
        hp_frac = hp / max_hp
        hostiles = self.perc.hostiles(t, 250.0)
        # A remembered-but-stale track is not a threat we can fight or run from: only
        # currently visible mechs drive the fight/evade decisions. Mixing the two left the
        # bot rotating aimlessly while a visible mech shot it from 90 m.
        fresh_hostiles = [(d, tr) for d, tr in hostiles if (t - tr.seen) < 0.5]
        threat = fresh_hostiles[0][1] if fresh_hostiles else None
        threat_d = fresh_hostiles[0][0] if fresh_hostiles else 1e9
        ghost = None
        if hostiles and (t - hostiles[0][1].seen) >= 0.5:
            ghost = hostiles[0]
        if self.k.get("target_weak") and fresh_hostiles:
            cands = [h for h in fresh_hostiles if h[0] < self.k["engage_dist"]]
            if cands:
                if self.k.get("target_ttk"):
                    # Pick the target we can actually finish soonest: cheap to kill, close,
                    # and in plain sight. Chasing the lowest-HP mech behind a wall burned the
                    # clock while the rest of the squad closed in.
                    dps = {"gun": 185.0, "laser": 170.0, "mortar": 70.0, "lightning": 235.0}
                    kind = self.combat.last_kind or "gun"
                    rate = dps.get(kind, 150.0)

                    def _ttk(h):
                        d, tr = h
                        hp = float(getattr(tr, "hp", 0.0) or 1400.0)
                        los = self.w.has_los(pos[0], pos[1], tr.x, tr.y)
                        return hp / rate + 0.0025 * d + (0.0 if los else 0.6)
                    threat_d, threat = min(cands, key=_ttk)
                else:
                    def _hp(h):
                        hp = getattr(h[1], "hp", None)
                        return hp if hp and hp > 0.0 else 1e9
                    threat_d, threat = min(cands, key=lambda h: _hp(h))
        fresh = threat is not None

        zone = obs["zone"]
        in_zone = bool(me.get("in_zone", True))
        zone_center = (zone["center"][0], zone["center"][1])
        next_center = (zone["next_center"][0], zone["next_center"][1])
        zr = float(zone["radius"])
        next_r = float(zone["next_radius"])

        # --- weapon pickup while a slot is free
        take_id, take_goal = self._pickup_step(obs, me, pos, threat_d, t)
        if not self.k.get("weapon_chase", 1):
            take_id, take_goal = None, None

        # --- camp spot
        camp_goal, camp_arrived = self._camp_step(obs, pos, t)

        # --- opening weapon rush
        rush_goal = self._rush_step(obs, pos, t, threat_d)

        # --- supply drop
        drop_goal, drop_id = self._drop_step(obs, pos, hp_frac, threat_d, t, int(me.get("cubes") or 0))

        # --- cube
        cube_goal = self._cube_step(obs, pos, hp_frac, threat_d)
        if not self.k.get("cube_chase", 1):
            cube_goal = None

        # --- farmable NPC drones and crates (cubes behind both)
        npc_d, npc_tr = self._npc_step(obs, t, threat_d, hp_frac)
        crate = self._crate_step(obs, pos, threat_d)

        mode = "ROTATE"
        goal = None
        k = self.k
        # Getting shredded right now? Break contact for a moment: standing in a three-way
        # crossfire is how an otherwise fine match ends at 90 s.
        self.hp_hist.append((t, hp_frac))
        while self.hp_hist and t - self.hp_hist[0][0] > 2.0:
            self.hp_hist.pop(0)
        if len(self.hp_hist) >= 3:
            dt = t - self.hp_hist[0][0]
            if dt >= 0.8 and (hp_frac - self.hp_hist[0][1]) * 1400.0 <= -float(k["panic_dmg"]) * dt:
                self.panic_until = max(self.panic_until, t + float(k["panic_s"]))
        panic = t < self.panic_until
        aimed = self._aimed_count(obs, pos, k["evade_aimed_dist"])
        tr_hp = float(getattr(threat, "hp", 0.0) or 0.0) if threat is not None else 0.0
        tr_frac = tr_hp / 1400.0 if tr_hp > 0.0 else 1.0
        # A hurt mech is what the house bots hunt, so disengage before we become the weakest.
        evade = (
            (hp_frac < k["evade_hp_frac"] and fresh and threat_d < k["evade_dist"])
            or (hp_frac < k["evade_aimed_hp"] and aimed > 0 and threat_d < k["evade_aimed_dist"])
            or (hp_frac < 0.5 and fresh and threat_d < 110.0)
            # a ghost that vanished behind cover 3 s ago is still worth backing away from
            or (ghost is not None and hp_frac < 0.5 and ghost[0] < 55.0)
            # opening seconds: the whole house squad hunts us, so survival beats damage
            or (t < k["early_evade_s"] and fresh and threat_d < k["evade_dist"])
            # two barrels on us at once is not a duel, it is a crossfire
            or (k["outnumber"] and len(fresh_hostiles) >= 2 and hp_frac < 0.80 and threat_d < 150.0)
            # we just lost a big chunk of armor in one window: duck out of the line of fire
            or (panic and hp_frac < 0.90)
        )
        if evade:
            self.evade_until = max(self.evade_until, t + float(k["evade_sticky"]))
            if threat is not None:
                self.evade_from = (threat.x, threat.y)
        elif (
            threat is not None
            and self.mode == "FIGHT"
            and hp_frac < k["trade_hp"]
            and tr_frac > k["trade_target_hp"]
            and (t - self.mode_t) > k["trade_dur"]
        ):
            # Losing the trade: the target is nearly full while we are bleeding out.
            self.evade_until = max(self.evade_until, t + 2.0)
            self.evade_from = (threat.x, threat.y)
        sticky = t < self.evade_until and hp_frac < 0.95 and self.evade_from is not None
        fight_ok = (
            hp_frac > k["engage_hp_frac"]
            and (tr_frac < k["engage_target_hp"] or hp_frac > k["engage_our_hp"])
        )
        heal = (
            k.get("heal")
            and hp_frac < k["heal_hp"]
            and hp_frac < k["heal_exit"]
            and (threat is None or (not fresh) or threat_d > k["heal_safe_dist"])
        )
        if not in_zone:
            mode = "ZONE"
            goal = self._zone_goal(obs, pos)
        elif (evade or sticky) and self.evade_from is not None:
            mode = "EVADE"
            if threat is not None and fresh:
                self.evade_from = (threat.x, threat.y)
            goal = self._evade_goal(pos, self.evade_from[0], self.evade_from[1], zone_center, zr)
        elif camp_goal is not None:
            mode = "CAMP"
            goal = None if camp_arrived else camp_goal
        elif rush_goal is not None:
            mode = "RUSH"
            goal = rush_goal
        elif drop_goal is not None:
            mode = "DROP"
            goal = drop_goal
        elif heal:
            mode = "HEAL"
            goal = self._cover_spot(pos, 70.0) or (
                pos[0] + math.cos(self.rng.uniform(0.0, 2.0 * math.pi)) * 30.0,
                pos[1] + math.sin(self.rng.uniform(0.0, 2.0 * math.pi)) * 30.0,
            )
        elif take_goal is not None and threat_d > 60.0:
            mode = "LOOT"
            goal = take_goal
        elif cube_goal is not None:
            mode = "LOOT"
            goal = cube_goal
        elif threat is not None and fresh and threat_d < k["engage_dist"] and fight_ok:
            mode = "FIGHT"
            goal = self._fight_goal(pos, threat, threat_d, t)
        elif npc_tr is not None:
            mode = "FARM"
            goal = self._farm_goal(pos, npc_tr, npc_d)
        elif crate is not None:
            mode = "FARM"
            goal = self._crate_goal(pos, crate)
        else:
            mode = "ROTATE"
            goal = self._rotate_goal(obs, pos, zone_center, next_center, next_r)

        if goal is not None:
            goal = self._avoid_wall(goal)
        if mode != self.mode:
            self.mode = mode
            self.mode_t = t

        # --- stuck handling: a ledge, a wall corner or a body in the way
        if self.last_pos is not None and self.goal is not None:
            moved = math.dist(pos[:2], self.last_pos)
            if moved < 0.25 and t - self.stuck_t > 0.9:
                self.stuck_n += 1
                self.stuck_t = t
                self.jump_until = t + 0.35
                if self.stuck_n >= 3:
                    self.stuck_n = 0
                    goal = self._detour(pos, zone_center, zr)
            elif moved > 1.5:
                self.stuck_n = 0
        self.last_pos = (pos[0], pos[1])
        self.goal = goal

        steer = self.nav.steer(pos, goal, t) if goal is not None else None
        move = [0.0, 0.0]
        dist_goal = 0.0
        if steer is not None:
            move = [steer[0], steer[1]]
            dist_goal = steer[2]

        # --- aim and fire
        aim = None
        combat_out = {}
        if threat is not None and fresh:
            combat_out = self.combat.decide(obs, threat, t)
        elif npc_tr is not None:
            combat_out = self.combat.decide(obs, npc_tr, t)
        elif crate is not None:
            combat_out = self._shoot_crate(obs, crate)
        if combat_out:
            aim = combat_out.get("aim")
            for _fld in ("fire", "reload"):
                if _fld in combat_out:
                    act[_fld] = combat_out[_fld]
        if aim is None:
            if threat is not None:
                tx, ty = threat.x, threat.y
                aim = [math.atan2(ty - pos[1], tx - pos[0]), 0.0]
            elif move[0] or move[1]:
                aim = [math.atan2(move[1], move[0]), 0.0]
            else:
                aim = [me["aim"][0], 0.0]

        # --- taking a weapon / cube pickup we are standing on
        if (
            take_id is None
            and getattr(self, "rush_target", None) is not None
            and math.dist(pos[:2], self.rush_target[1:]) <= self.pickup_r - 0.3
        ):
            act["take"] = self.rush_target[0]

        if take_id is not None and math.dist(pos[:2], take_goal) <= self.pickup_r - 0.3:
            act["take"] = take_id
            self.take_id = take_id
            self.take_until = t + 1.0

        if self.k.get("dodge", 1):
            ex, ey, urg = self.combat.dodge_vector(obs, me)
            if urg > 0.0:
                move = [move[0] + ex * (1.8 * urg), move[1] + ey * (1.8 * urg)]
                n = math.hypot(move[0], move[1])
                if n > 1.0:
                    move = [move[0] / n, move[1] / n]

        if self.k.get("jink") and math.hypot(move[0], move[1]) > 0.3 and fresh_hostiles:
            # Zig-zag: rounds fly at 252 m/s, so a target that changes heading twice a second
            # is far harder to lead than one walking a straight line.
            jx, jy = fresh_hostiles[0][1].x, fresh_hostiles[0][1].y
            if math.dist(pos[:2], (jx, jy)) < float(self.k["jink_dist"]):
                m = math.hypot(move[0], move[1])
                ux, uy = move[0] / m, move[1] / m
                a = float(self.k["jink_frac"]) * math.sin(2.0 * math.pi * float(self.k["jink_hz"]) * t)
                move = [ux - uy * a, uy + ux * a]
                m2 = math.hypot(move[0], move[1])
                if m2 > 1.0:
                    move = [move[0] / m2, move[1] / m2]

        act["move"] = move
        act["aim"] = [float(aim[0]), float(aim[1])]
        energy = float(me.get("energy", 0.0))
        want_boost = (
            energy > self.k["boost_energy"]
            and not self.combat.charging
            and (
                (not in_zone)
                or (mode in ("EVADE", "DROP", "LOOT", "RUSH") and dist_goal > 45.0)
                or (mode == "ROTATE" and dist_goal > 70.0)
                or (mode == "ZONE" and dist_goal > 30.0)
            )
        )
        act["boost"] = bool(want_boost)
        if t < self.jump_until:
            act["jump"] = True
        if self._debug_enabled:
            pass
        return act

    _debug_enabled = True

    # ------------------------------------------------------------------ zone
    def _zone_goal(self, obs, pos):
        z = obs["zone"]
        cx, cy = z["next_center"]
        r = float(z["next_radius"])
        d = math.hypot(cx - pos[0], cy - pos[1])
        if d <= r * 0.55:
            return (cx, cy)
        # aim a little inside the next circle, from the direction we come from
        f = max(0.0, (d - r * 0.45)) / d
        return (pos[0] + (cx - pos[0]) * f, pos[1] + (cy - pos[1]) * f)

    def _rotate_goal(self, obs, pos, center, next_center, next_r):
        z = obs["zone"]
        r = float(z["radius"])
        if r < 210.0:
            # late: hedge toward the next circle, but keep a little room to loot
            d = math.hypot(next_center[0] - pos[0], next_center[1] - pos[1])
            if d > next_r * 0.5:
                return next_center
        best = None
        best_d = 1e9
        for cube in obs.get("cubes") or []:
            d = math.dist(cube["pos"][:2], pos[:2])
            if d < best_d:
                best_d = d
                best = (cube["pos"][0], cube["pos"][1])
        if best is not None and best_d < 90.0:
            return best
        if self.k.get("cover"):
            spot = self._cover_spot(center, r)
            if spot is not None:
                return spot
        # wander inside the circle, biased to the centre
        ang = self.rng.uniform(0.0, 2.0 * math.pi)
        rad = r * self.rng.uniform(0.1, 0.5)
        return (center[0] + math.cos(ang) * rad, center[1] + math.sin(ang) * rad)

    def _cover_spot(self, center, r):
        """A walkable cell inside the circle that hugs scenery, so fewer guns have a line on it."""
        best = None
        best_score = -1.0
        for _ in range(24):
            ang = self.rng.uniform(0.0, 2.0 * math.pi)
            rad = r * self.rng.uniform(0.15, 0.55)
            x = center[0] + math.cos(ang) * rad
            y = center[1] + math.sin(ang) * rad
            if not self.w.walkable(x, y):
                continue
            score = float(self._cover_score(x, y))
            if not self.w.walkable(x + 6.0, y) and not self.w.walkable(x, y + 6.0):
                score -= 2.0
            if score > best_score:
                best_score = score
                best = (x, y)
        return best if best_score >= 3.0 else None

    def _cover_score(self, x, y):
        i, j = self.w.cell_of(x, y)
        n = 0
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                if di == 0 and dj == 0:
                    continue
                if self.w.sight_blocked_cell(i + di, j + dj):
                    n += 1
        return n

    # ------------------------------------------------------------------ looting
    def _pickup_step(self, obs, me, pos, threat_d, t):
        slots = me.get("weapons") or []
        if len(slots) < 2:
            return None, None
        empty = slots[1] is None
        if not empty:
            return None, None
        best = None
        best_s = -1.0
        for p in obs.get("pickups") or []:
            kind = p.get("kind")
            if kind is None or kind == "gun":
                continue
            d = math.dist(p["pos"][:2], pos[:2])
            if d > float(self.k.get("weapon_r") or 150.0):
                continue
            s = KIND_VALUE.get(kind, 0.0) * 100.0 - d
            if kind == "mortar":
                s -= 30.0
            if s > best_s:
                best_s = s
                best = p
        if best is None:
            return None, None
        if threat_d < float(self.k.get("weapon_threat") or 25.0):
            return None, None
        return best["id"], (best["pos"][0], best["pos"][1])

    def _camp_step(self, obs, pos, t):
        """Pick (once) and hold a quiet spot inside the next circle. Returns (goal, arrived)."""
        if not self.k.get("camp", 0):
            self.camp_spot = None
            return None, False
        zone = obs.get("zone") or {}
        nc = zone.get("next_center") or zone.get("center") or [0.0, 0.0]
        nr = float(zone.get("next_radius") or zone.get("radius") or 0.0)
        shrink_start = float(zone.get("shrink_start") or 0.0)
        frac = float(self.k.get("camp_frac") or 0.75)
        keep = max(nr * frac, 10.0)
        spot = getattr(self, "camp_spot", None)
        if spot is not None:
            if not self.w.inside(spot[0], spot[1]) or not self.w.walkable(spot[0], spot[1]):
                spot = None
            elif math.hypot(spot[0] - nc[0], spot[1] - nc[1]) > keep:
                spot = None
            else:
                travel = math.dist(pos[:2], spot) * 1.3 / 9.0
                if (
                    shrink_start
                    and math.dist(pos[:2], spot) > 6.0
                    and t + travel + float(self.k.get("camp_leave") or 12.0) >= shrink_start
                ):
                    spot = None
        if spot is None:
            best = None
            best_s = -1e9
            rng = self.rng
            for _ in range(28):
                ang = rng.uniform(0.0, 2.0 * math.pi)
                rad = math.sqrt(rng.random()) * keep
                gx = nc[0] + math.cos(ang) * rad
                gy = nc[1] + math.sin(ang) * rad
                if not self.w.inside(gx, gy) or not self.w.walkable(gx, gy):
                    continue
                d = math.dist(pos[:2], (gx, gy))
                if d > 110.0:
                    continue
                ci, cj = self.w.cell_of(gx, gy)
                cover = 0
                for di in (-2, 0, 2):
                    for dj in (-2, 0, 2):
                        if not self.w.sight_blocked_cell(ci + di, cj + dj):
                            cover += 1
                sc = cover * 1.6 - d * 0.08 - rad * 0.02
                if sc > best_s:
                    best_s = sc
                    best = (gx, gy)
            spot = best
            self.camp_spot = spot
        if spot is None:
            return None, False
        d = math.dist(pos[:2], spot)
        return spot, d <= 4.0

    def _rush_step(self, obs, pos, t, threat_d):
        """Opening seconds: grab a laser or lightning before the hunters find us."""
        self.rush_target = None
        if t >= float(self.k.get("rush_s") or 0.0):
            return None
        me = obs.get("self") or {}
        slots = me.get("weapons") or []
        if len(slots) < 2 or slots[1] is not None:
            return None
        if threat_d < 55.0:
            return None
        best = None
        best_s = -1e9
        reach = float(self.k.get("rush_dist") or 0.0)
        for p in obs.get("pickups") or ():
            kind = p.get("kind")
            if kind not in ("laser", "lightning"):
                continue
            d = math.dist(p["pos"][:2], pos[:2])
            if d > reach:
                continue
            sc = KIND_VALUE.get(kind, 0.0) * 100.0 - d
            if sc > best_s:
                best_s = sc
                best = p
        if best is None:
            return None
        self.rush_target = (best["id"], best["pos"][0], best["pos"][1])
        return (best["pos"][0], best["pos"][1])

    def _cube_step(self, obs, pos, hp_frac, threat_d):
        best = None
        best_d = 1e9
        for cube in obs.get("cubes") or []:
            d = math.dist(cube["pos"][:2], pos[:2])
            if d < best_d:
                best_d = d
                best = cube
        if best is None:
            return None
        if best_d > 70.0:
            return None
        if threat_d < best_d * 0.6 and threat_d < 90.0:
            return None
        return (best["pos"][0], best["pos"][1])

    def _drop_step(self, obs, pos, hp_frac, threat_d, t, cubes=0):
        best = None
        best_d = 1e9
        for drop in obs.get("drops") or []:
            lands_in = float(drop.get("lands_in") or 0.0)
            d = math.dist(drop["pos"][:2], pos[:2])
            if d > 210.0:
                continue
            # give up on a drop we cannot reach before it lands (fall time is 25 s)
            if lands_in > d / 18.0 + 1.0:
                continue
            if lands_in > 3.0 and d > 60.0:
                # By default we only chase a drop once it is about to land. With drop_early
                # we start moving right away, but only when the travel time fits the fall
                # window with a couple of seconds to spare.
                if not self.k.get("drop_early") or (d / 18.0) > lands_in - 2.0:
                    continue
            if hp_frac > 0.92 and int(cubes) >= 9 and lands_in > 0.5:
                continue
            holder = drop.get("holder")
            contested = holder is not None
            if not contested:
                for tr in self.perc.tracks.values():
                    if tr.kind == "mech" and math.dist((tr.x, tr.y), drop["pos"][:2]) < 10.0:
                        contested = True
                        break
            if contested and threat_d < 160.0 and d > 25.0:
                continue
            if d < best_d:
                best_d = d
                best = drop
        if best is None:
            return None, None
        if best_d > 8.0 and threat_d < best_d * 0.7 and threat_d < 110.0:
            return None, None
        return (best["pos"][0], best["pos"][1]), best["id"]

    # ------------------------------------------------------------------ farming
    def _npc_step(self, obs, t, threat_d, hp_frac):
        """Drones wander close and drop two cubes; farm one when nothing else is urgent."""
        if hp_frac < 0.5 or threat_d < self.k["farm_threat"]:
            return 1e9, None
        best = None
        best_d = 1e9
        for d, tr in self.perc.npcs(t, 95.0):
            if t - tr.seen > 0.5:
                continue
            if d < best_d:
                best_d = d
                best = tr
        if best is None:
            return 1e9, None
        return best_d, best

    def _crate_step(self, obs, pos, threat_d):
        if threat_d < self.k["crate_threat"]:
            return None
        gslot = self.combat.holding(obs["self"], "gun")
        if gslot is None:
            return None
        w = obs["self"]["weapons"][gslot]
        if float(w.get("ammo", 0)) < 4 and float(w.get("reload", 0)) <= 0.0:
            return None
        best = None
        best_d = 1e9
        for c in obs.get("crates") or []:
            d = math.dist(c["pos"][:2], pos[:2])
            if d > 95.0 or d < 6.0:
                continue
            if d < best_d and self.w.has_los(pos[0], pos[1], c["pos"][0], c["pos"][1]):
                best_d = d
                best = c
        return best

    def _farm_goal(self, pos, tr, d):
        want = 45.0
        if d > want + 10.0:
            return (tr.x, tr.y)
        if d < want - 10.0:
            ang = math.atan2(pos[1] - tr.y, pos[0] - tr.x)
            return (pos[0] + math.cos(ang) * 25.0, pos[1] + math.sin(ang) * 25.0)
        ang = math.atan2(tr.y - pos[1], tr.x - pos[0]) + math.pi * 0.5
        return (pos[0] + math.cos(ang) * 12.0, pos[1] + math.sin(ang) * 12.0)

    def _crate_goal(self, pos, crate):
        ang = math.atan2(pos[1] - crate["pos"][1], pos[0] - crate["pos"][0])
        return (crate["pos"][0] + math.cos(ang) * 28.0, crate["pos"][1] + math.sin(ang) * 28.0)

    def _shoot_crate(self, obs, crate):
        me = obs["self"]
        slot = self.combat.holding(me, "gun")
        if slot is None:
            return {}
        w = me["weapons"][slot]
        if float(w.get("ammo", 0)) <= 0.0:
            return {"reload": True} if float(w.get("reload", 0)) <= 0.0 else {}
        mx, my, mz = me["pos"]
        cx, cy, cz = crate["pos"]
        horiz = math.hypot(cx - mx, cy - my)
        if not self.w.has_los(mx, my, cx, cy):
            return {}
        muz = float((me.get("stats") or {}).get("muzzle_height", 6.5))
        yaw = math.atan2(cy - my, cx - mx)
        pitch = math.atan2(cz + 1.2 - (mz + muz), max(1.0, horiz))
        out = {"aim": [yaw, pitch]}
        if abs(_ang_diff(yaw, me["aim"][0])) < max(0.012, math.atan2(1.6, max(12.0, horiz))):
            fire = [False] * len(me["weapons"])
            fire[slot] = True
            out["fire"] = fire
        return out

    # ------------------------------------------------------------------ fight
    def _fight_goal(self, pos, tr, d, t):
        kind = self.combat.last_kind or "gun"
        pref = float(self.k["standoff"].get(kind, 120.0))
        fin = float(self.k.get("finish_hp") or 0.0)
        if fin > 0.0:
            hp = float(getattr(tr, "hp", 0.0) or 0.0)
            if hp > 0.0 and hp / 1400.0 < fin:
                pref = min(pref, float(self.k.get("finish_d") or 35.0))
        if d > pref + 45.0:
            return (tr.x, tr.y)
        if d < pref - 45.0:
            ang = math.atan2(pos[1] - tr.y, pos[0] - tr.x)
            return (pos[0] + math.cos(ang) * 45.0, pos[1] + math.sin(ang) * 45.0)
        if t - self.strafe_t > 1.4:
            self.strafe_t = t
            self.strafe_dir = -self.strafe_dir
        ang = math.atan2(tr.y - pos[1], tr.x - pos[0]) + self.strafe_dir * math.pi * 0.5
        return (pos[0] + math.cos(ang) * 22.0, pos[1] + math.sin(ang) * 22.0)

    def _evade_goal(self, pos, tx, ty, zone_center, zr):
        """Flee while breaking line of sight: cover beats raw distance."""
        base = math.atan2(pos[1] - ty, pos[0] - tx)
        step = float(self.k["evade_step"])
        best = None
        best_score = -1e9
        for i in range(-6, 7):
            ang = base + i * 0.26
            gx = pos[0] + math.cos(ang) * step
            gy = pos[1] + math.sin(ang) * step
            if not self.w.inside(gx, gy) or not self.w.walkable(gx, gy):
                continue
            if math.hypot(gx - zone_center[0], gy - zone_center[1]) > zr * 0.85:
                continue
            score = math.hypot(gx - tx, gy - ty) * 0.05
            if not self.w.has_los(gx, gy, tx, ty):
                score += 60.0
            if score > best_score:
                best_score = score
                best = (gx, gy)
        if best is not None:
            return best
        gx = pos[0] + math.cos(base) * step
        gy = pos[1] + math.sin(base) * step
        if math.hypot(gx - zone_center[0], gy - zone_center[1]) > zr * 0.85:
            return zone_center
        return (gx, gy)

    def _aimed_count(self, obs, pos, rng):
        """How many visible mechs have their torso pointed at us (a sign we are the target)."""
        n = 0
        for a in obs.get("mechs") or []:
            if float(a.get("hp") or 0.0) <= 0.0:
                continue
            ax, ay = a["pos"][0], a["pos"][1]
            dx, dy = ax - pos[0], ay - pos[1]
            d = math.hypot(dx, dy)
            if d > rng or d < 2.0:
                continue
            face = float((a.get("aim") or [0.0, 0.0])[0])
            if abs(_ang_diff(math.atan2(dy, dx), face)) < 0.10:
                n += 1
        return n

    # ------------------------------------------------------------------ helpers
    def _avoid_wall(self, goal):
        gx, gy = goal
        if not self.w.inside(gx, gy):
            gx = min(max(gx, self.w.min_x + 2.0), self.w.max_x - 2.0)
            gy = min(max(gy, self.w.min_y + 2.0), self.w.max_y - 2.0)
        if not self.w.walkable(gx, gy):
            n = self.w.nearest_walkable(gx, gy)
            if n is not None:
                return n
        return (gx, gy)

    def _detour(self, pos, center, zr):
        ang = self.rng.uniform(0.0, 2.0 * math.pi)
        d = self.rng.uniform(20.0, 45.0)
        gx = pos[0] + math.cos(ang) * d
        gy = pos[1] + math.sin(ang) * d
        if math.hypot(gx - center[0], gy - center[1]) > zr * 0.8:
            gx = center[0] + self.rng.uniform(-0.5, 0.5) * zr * 0.6
            gy = center[1] + self.rng.uniform(-0.5, 0.5) * zr * 0.6
        return (gx, gy)

    def _pick_upgrade(self, offer, me):
        choices = offer.get("choices") or []
        if not choices:
            return 0
        held = {(w or {}).get("kind") for w in (me.get("weapons") or [])}
        best_i = 0
        best_s = -1e9
        for i, ch in enumerate(choices):
            s = 0.0
            text = (str(ch.get("name", "")) + " " + str(ch.get("desc", "")) + " " + str(ch.get("id", ""))).lower()
            if "damage" in text:
                s += 2.5
            if "hp" in text or "health" in text:
                s += 2.0
            if "armor" in text:
                s += 2.0
            if "regen" in text:
                s += 1.4
            if "energy" in text:
                s += 0.9
            if "boost" in text or "jet" in text:
                s += 0.8
            if "speed" in text or "walk" in text:
                s += 0.8
            if "spread" in text or "accuracy" in text:
                s += float(1.6 if "gun" in held else 0.8)
            if "laser" in text and "laser" in held:
                s += 1.2
            if "mortar" in text and "mortar" in held:
                s += 1.2
            if "lightning" in text and "lightning" in held:
                s += 1.2
            if s > best_s:
                best_s = s
                best_i = i
        return best_i




