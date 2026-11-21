"""Behaviour: zone discipline, looting, fighting and escaping.

Ranking only cares about surviving to the end, so the policy spends the early match on
power cubes and a second weapon, keeps itself inside the shrinking circle, and avoids
fights it cannot win. Movement goals come from here; the navigator turns them into a
steering direction and the combat module turns a target into aim and trigger.
"""
import math
import random

KIND_VALUE = {"laser": 3.0, "mortar": 2.0, "lightning": 2.2, "gun": 0.2}


class Policy:
    def __init__(self, world, nav, perc, combat, cfg, seed=0):
        self.w = world
        self.nav = nav
        self.perc = perc
        self.combat = combat
        self.cfg = cfg
        self.rng = random.Random(seed)
        self.mode = "LOOT"
        self.mode_t = -99.0
        self.goal = None
        self.take_id = None
        self.take_until = -1.0
        self.strafe_dir = 1.0
        self.strafe_t = -99.0
        self.last_pos = None
        self.stuck_t = -99.0
        self.stuck_n = 0
        self.jump_until = -99.0
        self.pref = 0.0
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
        threat = hostiles[0][1] if hostiles else None
        threat_d = hostiles[0][0] if hostiles else 1e9
        fresh = threat is not None and (t - threat.seen) < 0.4

        zone = obs["zone"]
        in_zone = bool(me.get("in_zone", True))
        zone_center = (zone["center"][0], zone["center"][1])
        next_center = (zone["next_center"][0], zone["next_center"][1])
        zr = float(zone["radius"])
        next_r = float(zone["next_radius"])

        # --- weapon pickup while a slot is free
        take_id, take_goal = self._pickup_step(obs, me, pos, threat_d, t)

        # --- supply drop
        drop_goal, drop_id = self._drop_step(obs, pos, hp_frac, threat_d, t)

        # --- cube
        cube_goal = self._cube_step(obs, pos, hp_frac, threat_d)

        mode = "ROTATE"
        goal = None
        if not in_zone:
            mode = "ZONE"
            goal = self._zone_goal(obs, pos)
        elif hp_frac < 0.35 and fresh and threat_d < 130.0:
            mode = "EVADE"
            goal = self._evade_goal(pos, threat, zone_center, zr)
        elif drop_goal is not None:
            mode = "DROP"
            goal = drop_goal
        elif take_goal is not None and threat_d > 60.0:
            mode = "LOOT"
            goal = take_goal
        elif cube_goal is not None:
            mode = "LOOT"
            goal = cube_goal
        elif threat is not None and fresh and threat_d < 200.0 and hp_frac > 0.4:
            mode = "FIGHT"
            goal = self._fight_goal(pos, threat, threat_d, t)
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
        if combat_out:
            aim = combat_out.get("aim")
            for k in ("fire", "reload"):
                if k in combat_out:
                    act[k] = combat_out[k]
        if aim is None:
            if threat is not None:
                tx, ty = threat.x, threat.y
                aim = [math.atan2(ty - pos[1], tx - pos[0]), 0.0]
            elif move[0] or move[1]:
                aim = [math.atan2(move[1], move[0]), 0.0]
            else:
                aim = [me["aim"][0], 0.0]

        # --- taking a weapon / cube pickup we are standing on
        if take_id is not None and math.dist(pos[:2], take_goal) <= self.pickup_r - 0.3:
            act["take"] = take_id
            self.take_id = take_id
            self.take_until = t + 1.0

        act["move"] = move
        act["aim"] = [float(aim[0]), float(aim[1])]
        energy = float(me.get("energy", 0.0))
        want_boost = (
            energy > 22.0
            and not self.combat.charging
            and (
                (not in_zone)
                or (mode in ("EVADE", "DROP", "LOOT") and dist_goal > 45.0)
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
        # wander inside the circle, biased to the centre
        ang = self.rng.uniform(0.0, 2.0 * math.pi)
        rad = r * self.rng.uniform(0.1, 0.5)
        return (center[0] + math.cos(ang) * rad, center[1] + math.sin(ang) * rad)

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
            if d > 90.0:
                continue
            s = KIND_VALUE.get(kind, 0.0) * 100.0 - d
            if kind == "mortar":
                s -= 30.0
            if s > best_s:
                best_s = s
                best = p
        if best is None:
            return None, None
        if threat_d < 35.0:
            return None, None
        return best["id"], (best["pos"][0], best["pos"][1])

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

    def _drop_step(self, obs, pos, hp_frac, threat_d, t):
        best = None
        best_d = 1e9
        for drop in obs.get("drops") or []:
            lands_in = float(drop.get("lands_in") or 0.0)
            if lands_in > 1.0:
                continue
            holder = drop.get("holder")
            if holder is not None:
                continue
            d = math.dist(drop["pos"][:2], pos[:2])
            if d > 190.0:
                continue
            if hp_frac > 0.8 and int(drop.get("cubes") or 0) < 9:
                continue
            # contested: someone else is already standing in the ring
            contested = False
            for tr in self.perc.tracks.values():
                if tr.kind == "mech" and math.dist((tr.x, tr.y), drop["pos"][:2]) < 14.0:
                    contested = True
                    break
            if contested and threat_d < 140.0:
                continue
            if d < best_d:
                best_d = d
                best = drop
        if best is None:
            return None, None
        if best_d > 8.0 and threat_d < best_d * 0.8 and threat_d < 110.0:
            return None, None
        return (best["pos"][0], best["pos"][1]), best["id"]

    # ------------------------------------------------------------------ fight
    def _fight_goal(self, pos, tr, d, t):
        kind = self.combat.last_kind or "gun"
        pref = {"gun": 120.0, "laser": 160.0, "mortar": 150.0, "lightning": 28.0}.get(kind, 120.0)
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

    def _evade_goal(self, pos, threat, zone_center, zr):
        ang = math.atan2(pos[1] - threat.y, pos[0] - threat.x)
        gx = pos[0] + math.cos(ang) * 90.0
        gy = pos[1] + math.sin(ang) * 90.0
        if math.hypot(gx - zone_center[0], gy - zone_center[1]) > zr * 0.85:
            return zone_center
        return (gx, gy)

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
