"""Weapon choice, aim solving and fire gating.

Everything fires along the torso's *actual* aim, so the bot only pulls the trigger when the
slew error is small enough to hit. Shells are lobbed with the ballistic pitch; the laser is
charged and released over the target; lightning is only worth it inside its cone.
"""
import math

KIND_VALUE = {"laser": 3.0, "mortar": 2.2, "lightning": 1.9, "gun": 1.0}
PREF_RANGE = {"gun": 130.0, "laser": 175.0, "mortar": 160.0, "lightning": 30.0}
LASER_RELEASE = 0.62


def ang_diff(a, b):
    return (a - b + math.pi) % (2.0 * math.pi) - math.pi


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
        self.gravity = float(proj.get("gravity", 22.0))
        mech = cfg.get("mech") or {}
        self.muzzle_cfg = float(mech.get("muzzle_height", 6.5))
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

    def can_hit(self, kind, d, los):
        if kind == "gun":
            return d <= float(self.gun.get("range", 230.0)) * 1.02
        if kind == "laser":
            return d <= float(self.laser.get("range", 280.0)) and los
        if kind == "lightning":
            return d <= float(self.light.get("range", 45.0)) and los
        if kind == "mortar":
            v = float(self.mortar.get("speed", 72.0))
            return 20.0 <= d <= 0.98 * v * v / self.gravity
        return False

    def pick(self, me, d, los):
        """Best slot to shoot with at this target, or None."""
        light = self.holding(me, "lightning")
        if light is not None and self.can_hit("lightning", d, los):
            return light
        laser = self.holding(me, "laser")
        if laser is not None and self.can_hit("laser", d, los):
            return laser
        if laser is not None and d <= float(self.laser.get("range", 280.0)) * 0.95 and not los:
            # a laser cannot shoot through a wall; keep it for later
            pass
        mortar = self.holding(me, "mortar")
        if mortar is not None and not los and self.can_hit("mortar", d, False):
            return mortar
        gun = self.holding(me, "gun")
        if gun is not None and self.can_hit("gun", d, los):
            return gun
        if mortar is not None and self.can_hit("mortar", d, los) and d > 55.0:
            return mortar
        return None

    # ------------------------------------------------------------------ aiming
    def _muzzle(self, me):
        st = me.get("stats") or {}
        return float(st.get("muzzle_height", self.muzzle_cfg)), float(st.get("eye_height", self.eye_cfg))

    def aim_for(self, me, tr, kind, d):
        """Desired [yaw, pitch] to hit `tr` with `kind` from where we are."""
        mx, my, mz = me["pos"]
        muz, _ = self._muzzle(me)
        tz = tr.z + 4.0
        if kind == "gun":
            v = float(self.gun.get("speed", 252.0))
            t = d / max(1.0, v)
            for _ in range(3):
                t = math.hypot(tr.x + tr.vx * t - mx, tr.y + tr.vy * t - my) / max(1.0, v)
            px = tr.x + tr.vx * t
            py = tr.y + tr.vy * t
            pz = tz + tr.vz * t
        elif kind == "laser":
            px = tr.x + tr.vx * 0.04
            py = tr.y + tr.vy * 0.04
            pz = tz
        elif kind == "lightning":
            px, py, pz = tr.x, tr.y, tz
        else:  # mortar: ballistic pitch, no lead
            px, py, pz = tr.x + tr.vx * 0.25, tr.y + tr.vy * 0.25, tz
        dx = px - mx
        dy = py - my
        horiz = math.hypot(dx, dy)
        yaw = math.atan2(dy, dx)
        if kind == "mortar":
            v = float(self.mortar.get("speed", 72.0))
            g = self.gravity
            s = horiz * g / (v * v)
            if s > 0.995:
                s = 0.995
            pitch = 0.5 * math.asin(s)
            # small correction for the height difference, low-angle approximation
            pitch += math.atan2(pz - (mz + muz), max(4.0, horiz)) * 0.35
        else:
            pitch = math.atan2(pz - (mz + muz), max(1.0, horiz))
        return [yaw, pitch]

    # ------------------------------------------------------------------ shooting
    def decide(self, obs, tr, now):
        """Return the aim/fire part of an action against `tr` (or {} when we cannot shoot)."""
        me = obs["self"]
        mx, my, mz = me["pos"]
        d = math.hypot(tr.x - mx, tr.y - my)
        los = self.w.has_los(mx, my, tr.x, tr.y)
        self.charging = False
        slot = self.pick(me, d, los)
        if slot is None:
            self.last_kind = None
            return {}
        wpn = me["weapons"][slot]
        if not wpn:
            return {}
        kind = wpn["kind"]
        self.last_kind = kind
        aim = self.aim_for(me, tr, kind, d)
        err = abs(ang_diff(aim[0], me["aim"][0]))
        tol = max(0.010, math.atan2(1.5, max(12.0, d)))
        fire = [False] * len(me["weapons"])
        out = {"aim": aim}
        if kind == "gun":
            ammo = float(wpn.get("ammo", 0.0))
            reload_left = float(wpn.get("reload", 0.0))
            if ammo <= 0.0:
                if reload_left <= 0.0:
                    out["reload"] = True
            elif err < tol and los and d <= float(self.gun.get("range", 230.0)):
                fire[slot] = True
        elif kind == "laser":
            charge = float(wpn.get("charge", 0.0))
            cool = float(wpn.get("cooldown", 0.0))
            if cool > 0.01:
                pass
            elif charge > 0.02:
                self.charging = True
                if charge >= LASER_RELEASE and err < tol * 1.4:
                    fire[slot] = False     # release: the beam fires now
                elif err < 0.9:
                    fire[slot] = True      # keep charging while we slew onto the target
                else:
                    fire[slot] = False
            elif err < tol and los and d <= float(self.laser.get("range", 280.0)):
                fire[slot] = True
        elif kind == "lightning":
            energy = float(me.get("energy", 0.0))
            if energy >= 4.0 and d <= float(self.light.get("range", 45.0)) and err < 0.30 and los:
                fire[slot] = True
        elif kind == "mortar":
            cool = float(wpn.get("cooldown", 0.0))
            if cool <= 0.01 and err < tol * 2.2 and d > 45.0:
                fire[slot] = True
        if any(fire):
            out["fire"] = fire
        elif kind == "laser" and "fire" not in out:
            out["fire"] = fire            # sending False releases a held charge
        return out
