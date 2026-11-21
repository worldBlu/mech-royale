"""Weapon choice, aim solving and fire gating.

Everything fires along the torso's *actual* aim (`obs.self.aim`), not the commanded aim, so
the trigger is only pulled when the geometry says the shot would land: current aim error is
turned into a lateral/vertical miss in metres and compared with the target's body size.

Aim solutions, per weapon (all read from the protocol numbers and the per-observation
`stats`, because upgrades change muzzle height, speed and cooldowns):

* gun    - straight 252 m/s bullet, so lead the target; the shot spawns 3 m out along aim,
           which shortens the flight only (direction is unchanged);
* laser  - hitscan, so no lead at all; hold the trigger to charge, release only near full
           charge and only when actually on target (a release is the shot);
* mortar - ballistic: solve the low-angle pitch that makes the shell cross the target
           height, including the 3 m muzzle offset, then lead the target through the shell's
           time of flight (the old code used a fixed 0.25 s lead, which misses by ~15 m at
           100 m);
* lightning - instant, automatic: it hits the nearest target inside a 30 degree cone, so the
           gate is the cone, not metre-level aim.

Line of sight: the world grid treats tree trunks as sight blockers, but per protocol trees
stop vision, not shots.  A target the server lists in `obs.mechs` is visible right now, so
its line is clear for shooting even when the grid says otherwise; only when the target is
remembered (not currently visible) do we fall back to the grid and prefer the mortar.

Public API (used by policy.py / replay): pick, aim_for, decide, holding, can_hit,
last_kind, charging.
"""
import math

KIND_VALUE = {"laser": 3.0, "mortar": 2.2, "lightning": 1.9, "gun": 1.0}
PREF_RANGE = {"gun": 130.0, "laser": 175.0, "mortar": 160.0, "lightning": 30.0}

SHOT_ORIGIN = 3.0          # every shot spawns this far out along aim
LASER_RELEASE_FRAC = 0.96  # release at >=96% of max charge (0.72 s of 0.75 -> 315 dmg)
GUN_MARGIN_H = 2.1         # metres of lateral aim error we accept (mech radius 2.5)
GUN_MARGIN_V = 3.6         # metres of vertical aim error we accept (half height 4.5)
LASER_MARGIN_H = 2.0
LASER_MARGIN_V = 3.5
MORTAR_MARGIN_H = 4.5      # splash radius is 7 m; leave a little margin
MORTAR_MARGIN_RANGE = 5.5  # metres of range error accepted, from the pitch error
LIGHTNING_CONE_FRAC = 0.95
MORTAR_TUBE_EXTRA = 2.5    # mortar tube is pos.z+9.5, i.e. this much above the gun muzzle
MIN_MORTAR_RANGE = 20.0    # closer than this the 7 m splash can hurt us too


def ang_diff(a, b):
    return (a - b + math.pi) % (2.0 * math.pi) - math.pi


def _clamp(v, lo, hi):
    return lo if v < lo else (hi if v > hi else v)


class Combat:
    def __init__(self, world, cfg):
        self.w = world
        cw = cfg.get("weapons") or {}
        self.cfg = cw
        self.gun = cw.get("gun") or {}
        self.mortar = cw.get("mortar") or {}
        self.laser = cw.get("laser") or {}
        self.light = cw.get("lightning") or {}
        proj = cfg.get("projectile") or {}
        self.gravity = float(proj.get("gravity", self.mortar.get("gravity", 22.0)))
        mech = cfg.get("mech") or {}
        self.muzzle_cfg = float(mech.get("muzzle_height", 7.0))
        self.eye_cfg = float(mech.get("eye_height", 8.0))
        self.charging = False
        self.last_kind = None

    # ------------------------------------------------------------------ weapons
    @staticmethod
    def holding(me, kind):
        for i, w in enumerate(me.get("weapons") or []):
            if w and w.get("kind") == kind:
                return i
        return None

    def _base(self, kind):
        return {"gun": self.gun, "mortar": self.mortar,
                "laser": self.laser, "lightning": self.light}.get(kind) or {}

    def _wstats(self, wpn, kind):
        st = dict(self._base(kind))
        st.update((wpn or {}).get("stats") or {})
        return st

    def can_hit(self, kind, d, los):
        if kind == "gun":
            return d <= float(self.gun.get("range", 230.0)) * 1.02
        if kind == "laser":
            return d <= float(self.laser.get("range", 280.0)) and bool(los)
        if kind == "lightning":
            return d <= float(self.light.get("range", 45.0)) and bool(los)
        if kind == "mortar":
            v = float(self.mortar.get("speed", 72.0))
            return MIN_MORTAR_RANGE <= d <= 0.97 * v * v / self.gravity
        return False

    def _ready(self, wpn, kind):
        """Cooldown/charge state says this slot can shoot at all right now."""
        if kind == "laser":
            return (float(wpn.get("charge", 0.0)) > 0.02
                    or float(wpn.get("cooldown", 0.0)) <= 0.01)
        if kind == "lightning":
            return float(wpn.get("cooldown", 0.0)) <= 0.01
        if kind == "mortar":
            return float(wpn.get("cooldown", 0.0)) <= 0.01
        return True

    def _slot(self, me, kind):
        slot = self.holding(me, kind)
        if slot is None:
            return None
        weapons = me.get("weapons") or []
        if slot >= len(weapons) or not weapons[slot]:
            return None
        return slot, weapons[slot]

    def pick(self, me, d, los, tr=None):
        """Best slot to shoot with at this target, or None."""
        mx, my = float(me["pos"][0]), float(me["pos"][1])
        yaw = float((me.get("aim") or [0.0, 0.0])[0])

        laser = self._slot(me, "laser")
        if laser and self.can_hit("laser", d, los) and self._ready(laser[1], "laser"):
            return laser[0]

        light = self._slot(me, "lightning")
        if (light and self.can_hit("lightning", d, los)
                and self._ready(light[1], "lightning")):
            facing = True
            if tr is not None:
                bearing = math.atan2(float(tr.y) - my, float(tr.x) - mx)
                facing = abs(ang_diff(bearing, yaw)) <= 0.95
            if facing:
                return light[0]

        gun = self._slot(me, "gun")
        gun_empty = bool(gun and float(gun[1].get("ammo", 0.0) or 0.0) <= 0.0)
        if gun and not gun_empty and los and self.can_hit("gun", d, los):
            return gun[0]

        mortar = self._slot(me, "mortar")
        if (mortar and self._ready(mortar[1], "mortar")
                and self.can_hit("mortar", d, los) and d >= 25.0):
            if not los or d > 55.0:
                return mortar[0]

        if gun and gun_empty and los and self.can_hit("gun", d, los):
            return gun[0]        # decide() will ask for a reload
        return None

    # ------------------------------------------------------------------ aiming
    def _stats(self, me):
        return me.get("stats") or {}

    def _muzzle(self, me, kind="gun"):
        st = self._stats(me)
        muzzle = float(st.get("muzzle_height", self.muzzle_cfg))
        if kind == "mortar":
            muzzle += MORTAR_TUBE_EXTRA
        return muzzle

    def _limits(self, me):
        st = self._stats(me)
        return float(st.get("min_pitch", -0.7)), float(st.get("max_pitch", 1.3))

    @staticmethod
    def _target_center(tr):
        z = float(tr.z)
        if getattr(tr, "kind", "mech") == "npc":
            return z + 1.75
        cubes = float(getattr(tr, "cubes", 0.0) or 0.0)
        scale = max(1.0, 1.0 + 0.05 * cubes)
        return z + 4.5 * scale

    def _ballistic_pitch(self, x, dz, v, g):
        """Low-angle launch pitch hitting a point x ahead, dz above the tube.

        Returns (pitch, reachable).  Includes the 3 m shot-origin offset by iterating the
        effective start point; solves  a*u^2 - x*u + (dz+a) = 0  with a = g*x^2/(2v^2).
        """
        if g <= 0.0 or x <= 1.0:
            return math.atan2(dz, max(x, 0.5)), True
        pitch = 0.7
        ok = True
        for _ in range(3):
            x_eff = x - SHOT_ORIGIN * math.cos(pitch)
            z_eff = dz - SHOT_ORIGIN * math.sin(pitch)
            if x_eff <= 0.5:
                return math.atan2(z_eff, max(x_eff, 0.5)), False
            a = g * x_eff * x_eff / (2.0 * v * v)
            disc = x_eff * x_eff - 4.0 * a * (z_eff + a)
            if disc < 0.0:
                pitch = math.pi / 4.0
                ok = False
                # keep iterating with the 45 degree shot so the caller still gets a sane aim
                continue
            u = (x_eff - math.sqrt(disc)) / (2.0 * a)
            pitch = math.atan(u)
            ok = True
        return pitch, ok

    def aim_for(self, me, tr, kind, d, now=None):
        """Desired [yaw, pitch] to hit `tr` with `kind` from where we are.

        `now` is optional: when given, a stale track (`tr.seen < now`) is extrapolated to
        `now` before leading.  Kept backward compatible with the old 4-argument call.
        """
        mx, my, mz = (float(v) for v in me["pos"])
        seen = float(getattr(tr, "seen", now if now is not None else 0.0))
        if now is None:
            now = seen
        age = _clamp(now - seen, 0.0, 1.0)
        vx = float(getattr(tr, "vx", 0.0))
        vy = float(getattr(tr, "vy", 0.0))
        vz = float(getattr(tr, "vz", 0.0))
        # Extrapolate a stale track to `now`; visible tracks have age 0.
        tx = float(tr.x) + vx * age
        ty = float(tr.y) + vy * age
        tz = self._target_center(tr) + vz * age
        muzzle = self._muzzle(me, kind)
        gz = mz + muzzle

        if kind == "gun":
            v = float(self._base("gun").get("speed", 252.0))
            horiz = math.hypot(tx - mx, ty - my)
            t = max(0.0, math.hypot(horiz, tz - gz) - SHOT_ORIGIN) / max(1.0, v)
            px = py = pz = 0.0
            for _ in range(3):
                px = tx + vx * t
                py = ty + vy * t
                pz = tz + vz * t
                r = math.sqrt((px - mx) ** 2 + (py - my) ** 2 + (pz - gz) ** 2)
                t = max(0.0, r - SHOT_ORIGIN) / max(1.0, v)
            yaw = math.atan2(py - my, px - mx)
            pitch = math.atan2(pz - gz, max(1.0, math.hypot(px - mx, py - my)))
            return [yaw, pitch]

        if kind == "laser":
            # hitscan: no lead; aim at the target's current centre
            yaw = math.atan2(ty - my, tx - mx)
            pitch = math.atan2(tz - gz, max(1.0, math.hypot(tx - mx, ty - my)))
            return [yaw, pitch]

        if kind == "lightning":
            yaw = math.atan2(ty - my, tx - mx)
            pitch = math.atan2(tz - gz, max(1.0, math.hypot(tx - mx, ty - my)))
            return [yaw, pitch]

        # mortar: lead through the shell's flight, then solve the low-angle ballistic pitch
        v = float(self._base("mortar").get("speed", 72.0))
        px, py, pz = tx, ty, tz
        pitch = math.pi / 4.0
        for _ in range(4):
            horiz = math.hypot(px - mx, py - my)
            pitch, _ok = self._ballistic_pitch(horiz, pz - gz, v, self.gravity)
            tof = horiz / max(1.0, v * math.cos(pitch))
            px = tx + vx * tof
            py = ty + vy * tof
            pz = tz + vz * tof
        horiz = math.hypot(px - mx, py - my)
        pitch, _ok = self._ballistic_pitch(horiz, pz - gz, v, self.gravity)
        yaw = math.atan2(py - my, px - mx)
        return [yaw, pitch]

    # ------------------------------------------------------------------ shooting
    def decide(self, obs, tr, now):
        """Return the aim/fire part of an action against `tr` (or {} when we cannot shoot)."""
        me = obs["self"]
        weapons = me.get("weapons") or []
        self.charging = False
        mx, my, mz = (float(v) for v in me["pos"])

        visible_now = any(int(m.get("id", -1)) == int(tr.id)
                          for m in obs.get("mechs") or [])
        los = visible_now or bool(self.w.has_los(mx, my, tr.x, tr.y))
        d = math.hypot(float(tr.x) - mx, float(tr.y) - my)
        slot = self.pick(me, d, los, tr)
        if slot is None or slot >= len(weapons) or not weapons[slot]:
            self.last_kind = None
            return {}
        wpn = weapons[slot]
        kind = wpn["kind"]
        self.last_kind = kind
        wst = self._wstats(wpn, kind)
        desired = self.aim_for(me, tr, kind, d, now)
        min_pitch, max_pitch = self._limits(me)
        actual_yaw, actual_pitch = (me.get("aim") or [0.0, 0.0])[:2]
        yaw_err = abs(ang_diff(desired[0], float(actual_yaw)))
        pitch_err = abs(desired[1] - float(actual_pitch))
        in_pitch = min_pitch - 1e-6 <= desired[1] <= max_pitch + 1e-6

        horiz = math.hypot(float(tr.x) - mx, float(tr.y) - my)
        rng3 = math.hypot(horiz, self._target_center(tr) - (mz + self._muzzle(me, kind)))
        lateral = horiz * math.sin(min(yaw_err, math.pi / 2.0))
        vertical = rng3 * math.sin(min(pitch_err, math.pi / 2.0))

        aim_cmd = [desired[0], _clamp(desired[1], min_pitch, max_pitch)]
        fire = [False, False]
        out = {"aim": aim_cmd}

        if kind == "gun":
            ammo = float(wpn.get("ammo", 0.0) or 0.0)
            reload_left = float(wpn.get("reload", 0.0) or 0.0)
            cool = float(wpn.get("cooldown", 0.0) or 0.0)
            if ammo <= 0.0:
                if reload_left <= 0.0:
                    out["reload"] = True
            elif (reload_left <= 0.0 and cool <= 0.01 and los
                  and horiz <= float(wst.get("range", 230.0))
                  and in_pitch and lateral <= GUN_MARGIN_H and vertical <= GUN_MARGIN_V):
                fire[slot] = True

        elif kind == "laser":
            charge = float(wpn.get("charge", 0.0) or 0.0)
            cool = float(wpn.get("cooldown", 0.0) or 0.0)
            cmax = max(0.05, float(wst.get("charge", 0.75)))
            release = max(float(wst.get("min_charge", 0.15)), cmax * LASER_RELEASE_FRAC)
            if cool > 0.01:
                pass                                   # cooling down: charge is spent
            elif charge > 1e-3:
                self.charging = True
                aligned = (los and horiz <= float(wst.get("range", 280.0))
                           and in_pitch and lateral <= LASER_MARGIN_H
                           and vertical <= LASER_MARGIN_V)
                if charge >= release and aligned:
                    fire[slot] = False                 # release: this is the shot
                else:
                    fire[slot] = True                  # keep charging / wait for aim
            elif (los and horiz <= float(wst.get("range", 280.0)) and in_pitch):
                fire[slot] = True                      # start charging while we settle
            out["fire"] = fire                         # always explicit: False releases

        elif kind == "lightning":
            energy = float(me.get("energy", 0.0) or 0.0)
            cost = float(wst.get("energy", 3.0))
            cool = float(wpn.get("cooldown", 0.0) or 0.0)
            cone = math.radians(float(wst.get("cone_deg", 30.0)))
            if (los and cool <= 0.01 and energy >= cost
                    and horiz <= float(wst.get("range", 45.0))
                    and yaw_err <= cone * LIGHTNING_CONE_FRAC):
                fire[slot] = True

        elif kind == "mortar":
            cool = float(wpn.get("cooldown", 0.0) or 0.0)
            v = float(wst.get("speed", 72.0))
            reachable_pitch, reachable = self._ballistic_pitch(
                horiz, self._target_center(tr) - (mz + self._muzzle(me, "mortar")),
                v, self.gravity)
            # pitch error maps to range error through d(range)/d(pitch)
            sens = abs(2.0 * v * v / self.gravity * math.cos(2.0 * reachable_pitch))
            range_error = pitch_err * sens
            if (cool <= 0.01 and horiz >= MIN_MORTAR_RANGE and reachable
                    and in_pitch and lateral <= MORTAR_MARGIN_H
                    and range_error <= MORTAR_MARGIN_RANGE):
                fire[slot] = True

        if any(fire):
            out["fire"] = fire
        elif kind == "laser" and "fire" not in out:
            out["fire"] = fire
        return out
