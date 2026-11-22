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
#
# Skill knobs (all can be overridden per run with ``--house-knob key=value``; see
# ``tools/eval.py``).  Defaults were calibrated so the kit example bot sits near its real
# ladder score and the current bot lands near its real 48-match practice score:
#   aim_sigma_deg   gaussian aim error added to every shot (degrees, 1-sigma)
#   fire_tol_deg    only pull the trigger when the torso is within this angle of the aim
#                   solution (fire discipline; tighter = fewer, better shots)
#   burst_s         gun burst length before a forced pause (continuous fire if very large)
#   burst_pause_s   pause after a gun burst (burst discipline)
#   engage_m        max distance at which the house will fire at the player
#   pref_m          preferred orbit distance (smaller = houses close in harder)
#   react           delay between seeing the player and starting to shoot
#   strafe          seconds between strafe-direction flips
#   secondary       chance of starting with a looted secondary weapon
#   converge        movement speed factor while chasing the shared player track
LEVELS = {
    1: {
        "aim_sigma_deg": 4.42, "fire_tol_deg": 4.0, "burst_s": 99.0, "burst_pause_s": 0.0,
        "engage_m": 130.0, "pref_m": 85.0, "secondary": 0.15, "react": 0.9, "strafe": 2.2,
        "converge": 1.0,
    },
    2: {
        "aim_sigma_deg": 2.72, "fire_tol_deg": 2.8, "burst_s": 99.0, "burst_pause_s": 0.0,
        "engage_m": 165.0, "pref_m": 95.0, "secondary": 0.35, "react": 0.55, "strafe": 1.8,
        "converge": 1.0,
    },
    3: {
        "aim_sigma_deg": 1.53, "fire_tol_deg": 1.8, "burst_s": 99.0, "burst_pause_s": 0.0,
        "engage_m": 195.0, "pref_m": 105.0, "secondary": 0.60, "react": 0.3, "strafe": 1.5,
        "converge": 1.0,
    },
    4: {
        "aim_sigma_deg": 0.85, "fire_tol_deg": 1.1, "burst_s": 99.0, "burst_pause_s": 0.0,
        "engage_m": 215.0, "pref_m": 115.0, "secondary": 0.85, "react": 0.15, "strafe": 1.2,
        "converge": 1.0,
    },
}
LEVEL_COUNTS = (4, 4, 4, 3)  # easy, medium, hard, expert
DEFAULT_SKILL = {
    "aim_sigma_deg": 6.0, "fire_tol_deg": 9.2, "burst_s": 99.0, "burst_pause_s": 0.0,
    "engage_m": 120.0, "pref_m": 95.0, "secondary": 0.15, "react": 1.1, "strafe": 2.2,
    "converge": 1.0,
}

# Named presets.  "calibrated" is the default (LEVELS above, no team ping in vision mode).
# "harsh" models the builders-4 evidence: the private-scorer houses keep a stale lock on a
# mech for many seconds after it breaks line of sight (union of pings + last exact position),
# converge in a straight line with boost, and shoot noticeably straighter.  It is the preset
# to use when screening passive/camping policies, which the real houses punish.
PRESETS = {
    "calibrated": {"level": {}, "sense": {"shared_memory": 3.0}},
    "harsh": {
        "level": {
            "aim_sigma_scale": 0.55,
            "fire_tol_scale": 0.75,
            "react_scale": 0.40,
            "engage_add": 20.0,
            "pref_scale": 1.00,
            "close_when_blind": 1,
            "blind_mortar": 1,
            "converge_boost_energy": 6.0,
            "secondary_scale": 2.00,
            "damage_scale": 1.20,
        },
        "sense": {"shared_memory": 25.0, "ping_interval": 1.0, "ping_noise": 5.0},
    },
}


class HouseAI:
    def __init__(self, mech, level, rng, skill=None, preset=None):
        self.mech = mech
        self.level = level
        self.cfg = dict(LEVELS[level])
        preset = preset or {}
        for key, val in (preset.get("level") or {}).items():
            if key.endswith("_scale") and key[:-6] in self.cfg:
                self.cfg[key[:-6]] = float(self.cfg[key[:-6]]) * float(val)
            elif key.endswith("_add") and key[:-4] in self.cfg:
                self.cfg[key[:-4]] = float(self.cfg[key[:-4]]) + float(val)
            else:
                self.cfg[key] = val
        if skill:
            self.cfg.update(skill)
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
        self.search_goal = None
        self.search_until = 0.0
        self.burst_start = None
        self.burst_pause_until = -1.0
        self.burst_slot = None
        # angles are authored in degrees and converted once (clearer knobs, less unit confusion)
        self.err = math.radians(float(self.cfg["aim_sigma_deg"]))
        self.tol = math.radians(float(self.cfg["fire_tol_deg"]))

    # --------------------------------------------------------------- helpers
    def _set_move(self, mech, dx, dy, speed=1.0):
        d = math.hypot(dx, dy)
        if d < 0.5:
            mech.cmd_move[0] = 0.0
            mech.cmd_move[1] = 0.0
        else:
            mech.cmd_move[0] = dx / d * speed
            mech.cmd_move[1] = dy / d * speed

    def _toward(self, mech, tx, ty, stop=2.0, speed=1.0):
        dx = tx - mech.pos[0]
        dy = ty - mech.pos[1]
        d = math.hypot(dx, dy)
        if d <= stop:
            mech.cmd_move[0] = 0.0
            mech.cmd_move[1] = 0.0
        else:
            self._set_move(mech, dx, dy, speed)

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
            if abs(ang_diff(mech.aim[0], yaw)) <= self.tol:
                w.charge = float(w.stats["charge"])
                match.combat.fire_laser(mech, slot, error=0.0)
                return True
            return False
        yaw, pitch = match.combat.aim_at(mech, best.pos[0], best.pos[1],
                                         best.pos[2] + 1.75, kind, target=best)
        mech.cmd_aim[0] = yaw
        mech.cmd_aim[1] = pitch
        if abs(ang_diff(mech.aim[0], yaw)) < self.tol and abs(mech.aim[1] - pitch) < self.tol * 2:
            match.combat.fire(mech, slot, self.err)
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
        boost = False
        if not in_zone or edge_risk:
            gx, gy = self._zone_goal(match)
            self._toward(mech, gx, gy, 2.0, cfg["converge"])
        elif known is not None:
            dx = known[0] - mech.pos[0]
            dy = known[1] - mech.pos[1]
            dist = math.hypot(dx, dy)
            pref = cfg["pref_m"]
            if cfg.get("close_when_blind") and not visible:
                # Stale lock: keep closing on the last-known position until the target is
                # reacquired.  Once we are on top of it, orbit the spot so the search sweeps
                # around the cover the target is hiding behind (that is what punishes camping).
                if dist > 18.0:
                    self._toward(mech, known[0], known[1], 18.0, cfg["converge"])
                    boost = (bool(cfg.get("converge_boost", 1.0)) and dist > 45.0
                             and mech.energy > float(cfg.get("converge_boost_energy", 25.0)))
                else:
                    # Sweep around the last-known spot: random points within 10 m, refreshed
                    # every ~1.2 s, so the squad works its way around cover instead of orbiting
                    # a single bearing forever.
                    if self.search_goal is None or t > self.search_until:
                        a = self.rng.random() * math.tau
                        r = 2.0 + 8.0 * math.sqrt(self.rng.random())
                        self.search_goal = (known[0] + math.cos(a) * r,
                                            known[1] + math.sin(a) * r)
                        self.search_until = t + 1.2
                    self._toward(mech, self.search_goal[0], self.search_goal[1], 1.0,
                                 cfg["converge"])
            elif dist > pref + 30.0:
                self._toward(mech, known[0], known[1], 5.0, cfg["converge"])
                # converge faster: houses boost towards a shared sighting when far away
                boost = (bool(cfg.get("converge_boost", 1.0)) and dist > 60.0
                         and mech.energy > float(cfg.get("converge_boost_energy", 25.0)))
            elif dist < pref - 32.0:
                self._set_move(mech, -dx, -dy, cfg["converge"])
            else:
                # strafe orbit
                if t - self.strafe_t > cfg["strafe"]:
                    self.strafe_t = t
                    self.strafe_dir = -self.strafe_dir
                sd = self.strafe_dir
                self._set_move(mech, -dy * sd, dx * sd)
        else:
            gx, gy = self._zone_goal(match)
            self._toward(mech, gx, gy, 3.0, cfg["converge"])
        mech.cmd_boost = boost

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
            if player.alive and cfg.get("blind_mortar"):
                self._suppress(match)
            self._defend_against_npc(match, t)
            return
        tx = player.pos[0]
        ty = player.pos[1]
        tz = player.pos[2] + player.height * 0.5
        dist = math.hypot(tx - mech.pos[0], ty - mech.pos[1])
        los = match.line_clear(mech.pos, (tx, ty, tz))
        if dist > cfg["engage_m"]:
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
        if abs(ang_diff(mech.aim[0], yaw)) > self.tol:
            return
        if abs(mech.aim[1] - pitch) > self.tol * 1.5:
            return
        if kind == "gun":
            # burst discipline: fire a burst, then pause; only meaningful when burst_s is
            # finite (the calibrated default is continuous fire)
            if t < self.burst_pause_until:
                return
            if self.burst_slot != slot or self.burst_start is None:
                self.burst_slot = slot
                self.burst_start = t
            elif cfg["burst_pause_s"] > 0.0 and t - self.burst_start >= cfg["burst_s"]:
                self.burst_pause_until = t + cfg["burst_pause_s"]
                self.burst_start = None
                return
        else:
            self.burst_start = None
        match.combat.fire(mech, slot, self.err)

    def _suppress(self, match):
        """Lob a mortar shell at the squad's last-known player position (no LOS required).

        Real house bots are described as a coordinated squad; indirect fire on a stale lock is
        a plausible way they stop one camper from hiding for the whole match.  Damage still
        goes through the normal splash path, so being somewhere else is a full escape."""
        mech = self.mech
        known = match.house_player_pos
        if known is None:
            return False
        slot = mech.holding("mortar")
        if slot is None:
            return False
        w = mech.weapons[slot]
        t = match.t
        if w.cooldown > 0.0 or t < w.ready_at or mech.energy < 1.0:
            return False
        d = math.hypot(known[0] - mech.pos[0], known[1] - mech.pos[1])
        if not (55.0 <= d <= 205.0):
            return False
        kz = getattr(match, "house_player_z", None)
        if kz is None:
            kz = match.world.terrain_z(known[0], known[1])
        yaw, pitch = match.combat.aim_at(mech, known[0], known[1], kz + 1.0, "mortar")
        mech.cmd_aim[0] = yaw
        mech.cmd_aim[1] = pitch
        if abs(ang_diff(mech.aim[0], yaw)) > self.tol * 1.5:
            return False
        if abs(mech.aim[1] - pitch) > self.tol * 2.0:
            return False
        match.combat.fire(mech, slot, self.err)
        return True

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
        if abs(ang_diff(mech.aim[0], yaw)) > self.tol:
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
