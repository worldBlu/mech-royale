"""Scripted house bots: 4 easy, 4 medium, 4 hard, 3 expert.

The real server's house bots share vision, never attack each other and (per the track notes)
prefer the weakest player mech -- in this solo track that means they all hunt the bot under
test.  We do not know their exact AI, so this is an explicit, tunable approximation:

  * one team vision pool: if any house bot sees the player, all know the player's position;
  * they never fire at each other (house shots only collide with the player);
  * they orbit at a preferred range, strafe, and fire whatever weapon they carry;
  * difficulty controls aim error, engagement range and the chance of a starting secondary
    (the recorded stream already shows a house mech with a laser at t~8 s, so starting
    secondaries are needed to reproduce that pressure).
"""
import math

from .combat import ang_diff

# level: 1 easy, 2 medium, 3 hard, 4 expert
LEVELS = {
    1: {
        "err": 0.105, "engage": 120.0, "pref": 95.0, "tol": 0.16,
        "secondary": 0.15, "react": 1.1, "strafe": 2.2,
    },
    2: {
        "err": 0.062, "engage": 160.0, "pref": 112.0, "tol": 0.12,
        "secondary": 0.35, "react": 0.7, "strafe": 1.8,
    },
    3: {
        "err": 0.034, "engage": 205.0, "pref": 128.0, "tol": 0.085,
        "secondary": 0.60, "react": 0.45, "strafe": 1.5,
    },
    4: {
        "err": 0.017, "engage": 225.0, "pref": 140.0, "tol": 0.06,
        "secondary": 0.85, "react": 0.25, "strafe": 1.2,
    },
}
LEVEL_COUNTS = (4, 4, 4, 3)  # easy, medium, hard, expert


class HouseAI:
    def __init__(self, mech, level, rng):
        self.mech = mech
        self.level = level
        self.cfg = LEVELS[level]
        self.rng = rng
        self.last_pos = (mech.pos[0], mech.pos[1])
        self.stuck_t = 0.0
        self.detour_until = -1.0
        self.detour_dir = (0.0, 0.0)
        self.strafe_dir = 1.0 if rng.random() < 0.5 else -1.0
        self.strafe_t = 0.0
        self.charge_slot = None
        self.charge_until = 0.0
        self.charge_charge = 0.0
        self.seen_since = -1e9
        self.was_visible = False
        self.loot_want = None

    # --------------------------------------------------------------- helpers
    def _set_move(self, mech, dx, dy):
        d = math.hypot(dx, dy)
        if d < 0.5:
            mech.cmd_move[0] = 0.0
            mech.cmd_move[1] = 0.0
        else:
            mech.cmd_move[0] = dx / d
            mech.cmd_move[1] = dy / d

    def _toward(self, mech, tx, ty, stop=2.0):
        dx = tx - mech.pos[0]
        dy = ty - mech.pos[1]
        d = math.hypot(dx, dy)
        if d <= stop:
            mech.cmd_move[0] = 0.0
            mech.cmd_move[1] = 0.0
        else:
            self._set_move(mech, dx, dy)

    def _zone_goal(self, match):
        z = match.zone
        # If the next circle is far and the current one is comfortable, drift to it first.
        mech = self.mech
        d_cur = z.dist(mech.pos[0], mech.pos[1])
        d_next = z.dist_to_next(mech.pos[0], mech.pos[1])
        if d_cur > z.radius * 0.88:
            return z.center[0], z.center[1]
        if match.t < z.shrink_start and d_next > z.next_radius * 0.85:
            return z.next_center[0], z.next_center[1]
        return z.center[0], z.center[1]

    def _defend_against_npc(self, match, t):
        """House bots shoot back at NPC drones; otherwise free NPC damage would kill them
        and distort the survival order.  (TODO: real house-bot NPC handling is unknown.)"""
        mech = self.mech
        best = None
        bd = 55.0
        for npc in match.npcs:
            if not npc.alive:
                continue
            d = math.hypot(npc.pos[0] - mech.pos[0], npc.pos[1] - mech.pos[1])
            if d < bd and match.line_clear(mech.pos, npc.pos):
                bd = d
                best = npc
        if best is None:
            return False
        slot = None
        kind = None
        for k in ("laser", "gun", "lightning"):
            s = mech.holding(k)
            if s is None:
                continue
            w = mech.weapons[s]
            rng = float(w.stats["range"])
            if k == "lightning":
                rng = min(20.0, rng)
            if bd <= rng * 0.9 and w.cooldown <= 0.0 and mech.energy >= float(w.stats.get("energy", 0)):
                slot, kind = s, k
                break
        if slot is None:
            return False
        w = mech.weapons[slot]
        if kind == "laser":
            if match.t < w.short_until:
                return False
            yaw, pitch = match.combat.aim_at(mech, best.pos[0], best.pos[1],
                                             best.pos[2] + 1.75, "laser", target=best)
            mech.cmd_aim[0] = yaw
            mech.cmd_aim[1] = pitch
            if abs(ang_diff(mech.aim[0], yaw)) <= self.cfg["tol"]:
                w.charge = float(w.stats["charge"])
                match.combat.fire_laser(mech, slot, error=0.0)
                return True
            return False
        yaw, pitch = match.combat.aim_at(mech, best.pos[0], best.pos[1],
                                         best.pos[2] + 1.75, kind, target=best)
        mech.cmd_aim[0] = yaw
        mech.cmd_aim[1] = pitch
        if abs(ang_diff(mech.aim[0], yaw)) < self.cfg["tol"] and abs(mech.aim[1] - pitch) < self.cfg["tol"] * 2:
            match.combat.fire(mech, slot, self.cfg["err"])
            return True
        return False

    # --------------------------------------------------------------- think
    def think(self, match):
        mech = self.mech
        if not mech.alive:
            return
        t = match.t
        cfg = self.cfg
        player = match.player
        known = match.house_player_pos
        visible = match.house_player_visible and player.alive
        if visible and not self.was_visible:
            self.seen_since = t
        self.was_visible = visible
        if known is not None and not visible:
            # Face the last known position: the vision cone is where we aim, so a house
            # that chases without turning is blind until the 45 m proximity ring.
            mech.cmd_aim[0] = math.atan2(known[1] - mech.pos[1], known[0] - mech.pos[0])
            mech.cmd_aim[1] = 0.0

        # ---------- zone pressure first
        dzone = match.zone.dist(mech.pos[0], mech.pos[1])
        in_zone = match.zone.in_zone(mech.pos[0], mech.pos[1], margin=-2.0)
        edge_risk = match.zone.radius > 30.0 and dzone > match.zone.radius - 12.0
        if not in_zone or edge_risk:
            gx, gy = self._zone_goal(match)
            self._toward(mech, gx, gy, 2.0)
        elif known is not None:
            dx = known[0] - mech.pos[0]
            dy = known[1] - mech.pos[1]
            dist = math.hypot(dx, dy)
            pref = cfg["pref"]
            if dist > pref + 30.0:
                self._toward(mech, known[0], known[1], 5.0)
            elif dist < pref - 32.0:
                self._set_move(mech, -dx, -dy)
            else:
                # strafe orbit
                if t - self.strafe_t > cfg["strafe"]:
                    self.strafe_t = t
                    self.strafe_dir = -self.strafe_dir
                sd = self.strafe_dir
                self._set_move(mech, -dy * sd, dx * sd)
        else:
            gx, gy = self._zone_goal(match)
            self._toward(mech, gx, gy, 3.0)

        # ---------- stuck detection: take a random detour for a moment
        moved = math.dist((mech.pos[0], mech.pos[1]), self.last_pos)
        if moved < 0.2 and t - self.stuck_t > 1.2:
            self.stuck_t = t
            self.detour_until = t + 1.2
            a = self.rng.random() * math.tau
            self.detour_dir = (math.cos(a), math.sin(a))
        if t < self.detour_until:
            if moved > 1.0:
                self.detour_until = -1.0
            else:
                mech.cmd_move[0] = self.detour_dir[0]
                mech.cmd_move[1] = self.detour_dir[1]
        if moved > 2.0:
            self.stuck_t = t
        self.last_pos = (mech.pos[0], mech.pos[1])

        # ---------- aim / fire
        if not visible or not player.alive:
            if self.charge_slot is not None:
                self._abort_charge(mech)
            self._defend_against_npc(match, t)
            return
        tx = player.pos[0]
        ty = player.pos[1]
        tz = player.pos[2] + player.height * 0.5
        dist = math.hypot(tx - mech.pos[0], ty - mech.pos[1])
        los = match.line_clear(mech.pos, (tx, ty, tz))
        if dist > cfg["engage"]:
            self._defend_against_npc(match, t)
            return
        if t - self.seen_since < cfg["react"]:
            return

        kind, slot = self._pick_weapon(match, dist, los)
        if slot is None:
            return
        if kind == "laser":
            self._drive_laser(match, slot, tx, ty, tz, dist)
            return
        self._abort_charge(mech)
        yaw, pitch = match.combat.aim_at(mech, tx, ty, tz, kind, target=player)
        mech.cmd_aim[0] = yaw
        mech.cmd_aim[1] = pitch
        if abs(ang_diff(mech.aim[0], yaw)) > cfg["tol"]:
            return
        if abs(mech.aim[1] - pitch) > cfg["tol"] * 1.5:
            return
        match.combat.fire(mech, slot, cfg["err"])

    def _drive_laser(self, match, slot, tx, ty, tz, dist):
        mech = self.mech
        w = mech.weapons[slot]
        if w is None:
            return
        yaw, pitch = match.combat.aim_at(mech, tx, ty, tz, "laser", target=match.player)
        mech.cmd_aim[0] = yaw
        mech.cmd_aim[1] = pitch
        if w.cooldown > 0 or match.t < w.ready_at or match.t < w.short_until:
            self._abort_charge(mech)
            return
        if abs(ang_diff(mech.aim[0], yaw)) > self.cfg["tol"]:
            return
        if self.charge_slot is None:
            self.charge_slot = slot
            self.charge_charge = 0.55 + self.rng.random() * 0.2
            self.charge_until = match.t + self.charge_charge
            w.charge = 0.001
        w.charge = min(float(w.stats["charge"]), w.charge + 0.2)
        if match.t >= self.charge_until:
            match.combat.fire_laser(mech, slot)
            self.charge_slot = None
            self.charge_until = 0.0

    def _abort_charge(self, mech):
        if self.charge_slot is not None:
            w = mech.weapons[self.charge_slot]
            if w is not None:
                w.charge = 0.0
            self.charge_slot = None
            self.charge_until = 0.0

    def _pick_weapon(self, match, dist, los):
        mech = self.mech
        t = match.t
        # lightning: close and visible
        slot = mech.holding("lightning")
        if slot is not None:
            w = mech.weapons[slot]
            if (
                dist <= min(42.0, float(w.stats["range"]) * 0.95)
                and los
                and w.cooldown <= 0.0
                and mech.energy >= float(w.stats.get("energy", 3))
            ):
                return "lightning", slot
        # laser: mid range, needs line of sight
        slot = mech.holding("laser")
        if slot is not None:
            w = mech.weapons[slot]
            if (
                25.0 <= dist <= float(w.stats["range"]) * 0.95
                and los
                and w.cooldown <= 0.0
                and t >= w.short_until
                and t >= w.ready_at
            ):
                return "laser", slot
        # mortar: lob at distance, works without line of sight
        slot = mech.holding("mortar")
        if slot is not None:
            w = mech.weapons[slot]
            if 55.0 <= dist <= 205.0 and w.cooldown <= 0.0 and mech.energy > 1.0:
                return "mortar", slot
        # gun fallback
        w = mech.weapons[0]
        if w is not None and w.kind == "gun":
            if dist <= float(w.stats["range"]) * 0.97 and los and w.cooldown <= 0.0 and w.reload <= 0.0 and w.ammo > 0:
                return "gun", 0
        return None, None
