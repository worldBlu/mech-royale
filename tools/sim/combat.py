"""Weapon firing and hit resolution.

Implements the four kit weapons as described by kit/protocol.md 297-320 and the numbers in
hello.config.weapons:

  gun       12-round magazine, 0.24 s cooldown, straight 252 m/s bullets, 230 m range
  mortar    ballistic 72 m/s shell, gravity 22, impact + 7 m splash, concussion knockback
  laser     hold-to-charge hitscan, 0.15..0.75 s charge, 35%..100% of 315 damage, release to
            fire, auto-fire after hold_s, 280 m range, cook-off vs a carrier with a mortar
  lightning instant 45 m / 30 deg cone, 60 damage, 2 chains at 0.7x, 3 energy, short-circuit

House-rule fidelity: house bot shots target the player only and never collide with other
house mechs (the server's house bots never attack each other).  NPC bullets may hit anyone.
"""
import math

from .entities import Projectile

TAU = 2.0 * math.pi


def ang_diff(a, b):
    d = (a - b) % TAU
    if d > math.pi:
        d -= TAU
    return d


def _clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


class CombatEngine:
    def __init__(self, match):
        self.m = match
        self.cfg = match.cfg
        self.W = self.cfg["weapons"]
        self.rng = match.rng
        self.arena = match.arena
        self.proj_cfg = self.cfg["projectile"]
        self._next_id = 30000

    # ------------------------------------------------------------------ helpers
    def _pid(self):
        self._next_id += 1
        return self._next_id

    @staticmethod
    def direction(yaw, pitch):
        cp = math.cos(pitch)
        return [cp * math.cos(yaw), cp * math.sin(yaw), math.sin(pitch)]

    def muzzle(self, mech, yaw=None, pitch=None):
        yaw = mech.aim[0] if yaw is None else yaw
        off = mech.radius + 0.5
        return [
            mech.pos[0] + math.cos(yaw) * off,
            mech.pos[1] + math.sin(yaw) * off,
            mech.pos[2] + mech.muzzle_height,
        ]

    def is_rear(self, target, from_point):
        if target is None or not hasattr(target, "aim"):
            return False
        yaw = math.atan2(from_point[1] - target.pos[1], from_point[0] - target.pos[0])
        arc = math.radians(float(target.stats.get("front_arc_deg", 180.0))) * 0.5
        return abs(ang_diff(yaw, target.aim[0])) > arc

    def targets_for(self, owner):
        """Mechs a shot from ``owner`` may damage (besides NPCs/crates)."""
        out = []
        house = isinstance(owner, object) and getattr(owner, "house", False)
        for mech in self.m.mechs:
            if not mech.alive or mech is owner:
                continue
            if house and mech.house:
                continue  # house bots never attack each other
            out.append(mech)
        return out

    # ------------------------------------------------------------------ firing
    def fire(self, mech, slot, error=0.0):
        w = mech.weapons[slot] if 0 <= slot < 2 else None
        if w is None or not mech.alive:
            return False
        if self.m.t < w.ready_at:
            return False
        if w.kind == "gun":
            return self._fire_gun(mech, w, error)
        if w.kind == "mortar":
            return self._fire_mortar(mech, w, error)
        if w.kind == "lightning":
            return self._fire_lightning(mech, w, error)
        # laser is driven by Match.update_weapons (charge/release), not by a pull
        return False

    def _fire_gun(self, mech, w, error):
        if w.cooldown > 0.0 or w.reload > 0.0 or w.ammo <= 0:
            return False
        stats = w.stats
        spread = math.radians(float(stats.get("spread_deg", 0.0)))
        yaw = mech.aim[0] + self.rng.gauss(0.0, spread) + self.rng.gauss(0.0, error)
        pitch = mech.aim[1] + self.rng.gauss(0.0, spread) + self.rng.gauss(0.0, error * 0.5)
        origin = self.muzzle(mech, yaw, pitch)
        if self.arena.solid_point(*origin):
            return False
        d = self.direction(yaw, pitch)
        speed = float(stats["speed"])
        p = Projectile(
            self._pid(), "gun", origin,
            [d[0] * speed, d[1] * speed, d[2] * speed],
            mech.id, mech.house, float(stats["damage"]) * mech.damage_mult(),
            self.proj_cfg["gun_radius"],
        )
        p.ttl = float(stats["range"]) / speed + 0.5
        p.owner_kind = "mech"
        self.m.projectiles.append(p)
        w.ammo -= 1
        w.cooldown = float(stats["cooldown"])
        mech.shots += 1
        if w.ammo <= 0:
            w.reload = float(stats["reload"])
        self.m.sound("gun", origin[0], origin[1], mech)
        return True

    def _fire_mortar(self, mech, w, error):
        if w.cooldown > 0.0:
            return False
        stats = w.stats
        drain = float(stats.get("drain", 0.0))
        if mech.energy <= 0.5:
            return False
        mech.energy = max(0.0, mech.energy - min(drain, mech.energy))
        mech.last_energy_t = self.m.t
        yaw = mech.aim[0] + self.rng.gauss(0.0, error)
        pitch = mech.aim[1] + self.rng.gauss(0.0, error * 0.5)
        origin = [
            mech.pos[0] + math.cos(yaw) * (mech.radius + 0.5),
            mech.pos[1] + math.sin(yaw) * (mech.radius + 0.5),
            mech.pos[2] + mech.stats["height"] * 0.95 * mech.size,
        ]
        if self.arena.solid_point(*origin):
            return False
        d = self.direction(yaw, pitch)
        speed = float(stats["speed"])
        p = Projectile(
            self._pid(), "mortar", origin,
            [d[0] * speed, d[1] * speed, d[2] * speed],
            mech.id, mech.house, float(stats["damage"]) * mech.damage_mult(), 0.3,
        )
        p.gravity = float(stats["gravity"])
        p.ttl = float(stats.get("ttl", 7.0))
        p.splash = float(stats.get("splash", 0.0)) * mech.damage_mult()
        p.splash_radius = float(stats.get("splash_radius", 0.0))
        p.splash_edge = float(stats.get("splash_edge", 0.3))
        p.knock = float(stats.get("knock", 0.0))
        p.knock_up = float(stats.get("knock_up", 0.0))
        p.drain = float(stats.get("drain", 0.0))
        p.owner_kind = "mech"
        self.m.projectiles.append(p)
        w.cooldown = float(stats["cooldown"])
        mech.shots += 1
        self.m.sound("mortar", origin[0], origin[1], mech)
        return True

    # ------------------------------------------------------------------ laser
    def fire_laser(self, mech, slot, error=0.0):
        w = mech.weapons[slot] if 0 <= slot < 2 else None
        if w is None or w.kind != "laser" or not mech.alive:
            return False
        t = self.m.t
        if w.cooldown > 0.0 or t < w.ready_at or t < w.short_until:
            return False
        stats = w.stats
        full = float(stats["charge"])
        frac = float(stats["min_frac"]) + (1.0 - float(stats["min_frac"])) * min(w.charge, full) / full
        dmg = float(stats["damage"]) * frac * mech.damage_mult()
        yaw = mech.aim[0] + self.rng.gauss(0.0, error)
        pitch = mech.aim[1] + self.rng.gauss(0.0, error * 0.5)
        pitch = _clamp(pitch, float(mech.stats.get("min_pitch", -0.7)),
                       float(mech.stats.get("max_pitch", 1.3)))
        origin = self.muzzle(mech, yaw)
        d = self.direction(yaw, pitch)
        max_range = float(stats["range"])
        end = [origin[0] + d[0] * max_range, origin[1] + d[1] * max_range, origin[2] + d[2] * max_range]
        world = self.arena.raycast(origin, end)
        wall_t = 1.0 if world is None else world[0]
        seg = end
        hits = []
        for mech2 in self.targets_for(mech):
            tt = self.arena.ray_cylinder(
                origin, seg, mech2.pos[0], mech2.pos[1], mech2.pos[2],
                mech2.radius, mech2.height,
            )
            if tt is not None and tt <= wall_t:
                hits.append((tt, "mech", mech2))
        for npc in self.m.npcs:
            if not npc.alive:
                continue
            tt = self.arena.ray_cylinder(
                origin, seg, npc.pos[0], npc.pos[1], npc.pos[2], npc.radius, npc.height
            )
            if tt is not None and tt <= wall_t:
                hits.append((tt, "npc", npc))
        for crate in self.m.crates:
            if crate["hp"] <= 0:
                continue
            tt = self.arena.ray_cylinder(
                origin, seg, crate["pos"][0], crate["pos"][1], crate["pos"][2],
                float(self.cfg["crates"]["radius"]), float(self.cfg["crates"]["height"]),
            )
            if tt is not None and tt <= wall_t:
                hits.append((tt, "crate", crate))
        hits.sort(key=lambda h: h[0])
        pierce = int(stats.get("pierce", 0))
        used = 0
        end_point = [origin[i] + (seg[i] - origin[i]) * wall_t for i in range(3)]
        last_t = wall_t
        for tt, kind, obj in hits:
            if used > pierce:
                break
            point = [origin[i] + (seg[i] - origin[i]) * tt for i in range(3)]
            if kind == "mech":
                if self.m.has_mortar(obj) and obj is not None:
                    dmg2 = dmg * (1.0 + float(stats.get("cookoff", 0.6)))
                else:
                    dmg2 = dmg
                rear = self.is_rear(obj, origin)
                self.m.apply_damage(obj, dmg2, "laser", mech, point, rear=rear)
                if self.m.has_mortar(obj):
                    self.m.player_effect(obj, "cookoff", mech.id)
            elif kind == "npc":
                self.m.apply_damage(obj, dmg, "laser", mech, point, rear=False)
            else:
                self.m.apply_damage(obj, dmg, "laser", mech, point, rear=False)
            end_point = point
            last_t = tt
            used += 1
        cooldown = float(stats["cooldown"])
        w.cooldown = cooldown
        w.charge = 0.0
        w.hold = 0.0
        mech.shots += 1
        start = origin
        self.m.add_beam(mech.id, "laser", [start, end_point])
        self.m.sound("laser", origin[0], origin[1], mech)
        return True

    # ------------------------------------------------------------------ lightning
    def _lightning_targets(self, mech, yaw):
        stats = self.W["lightning"]
        rng_range = float(mech.weapons[mech.holding("lightning")].stats["range"])
        cone = math.radians(float(mech.weapons[mech.holding("lightning")].stats["cone_deg"])) * 0.5
        out = []
        for obj in self.m.mech_targets_for_lightning(mech):
            dx = obj.pos[0] - mech.pos[0]
            dy = obj.pos[1] - mech.pos[1]
            d = math.hypot(dx, dy)
            if d > rng_range or d < 0.5:
                continue
            if abs(ang_diff(math.atan2(dy, dx), yaw)) > cone:
                continue
            out.append((d, obj))
        out.sort(key=lambda p: p[0])
        return out

    def _fire_lightning(self, mech, w, error):
        stats = w.stats
        if w.cooldown > 0.0:
            return False
        cost = float(stats.get("energy", 0.0))
        if mech.energy < cost:
            return False
        yaw = mech.aim[0] + self.rng.gauss(0.0, error)
        targets = self._lightning_targets(mech, yaw)
        mech.energy = max(0.0, mech.energy - cost)
        mech.last_energy_t = self.m.t
        w.cooldown = float(stats["cooldown"])
        mech.shots += 1
        origin = self.muzzle(mech, yaw)
        chain = int(stats.get("chain", 0))
        chain_range = float(stats.get("chain_range", 0.0))
        falloff = float(stats.get("chain_falloff", 0.7))
        path = [origin]
        picked = []
        if targets:
            first = targets[0][1]
            picked.append(first)
            path.append([first.pos[0], first.pos[1], first.pos[2] + 2.0])
            dmg = float(stats["damage"]) * mech.damage_mult()
            self.m.apply_damage(first, dmg, "lightning", mech, origin, rear=False)
            self.m.apply_short(first, self.m.t, float(stats.get("short_s", 2.0)), mech.id)
            last = first
            cur_dmg = dmg
            for _ in range(chain):
                nxt = None
                best = chain_range
                for obj in self.m.mech_targets_for_lightning(mech):
                    if obj in picked:
                        continue
                    dd = math.dist((last.pos[0], last.pos[1]), (obj.pos[0], obj.pos[1]))
                    if dd < best and self.m.line_clear(last.pos, obj.pos):
                        best = dd
                        nxt = obj
                if nxt is None:
                    break
                picked.append(nxt)
                cur_dmg *= falloff
                path.append([nxt.pos[0], nxt.pos[1], nxt.pos[2] + 2.0])
                self.m.apply_damage(nxt, cur_dmg, "lightning", mech, path[-2], rear=False)
                self.m.apply_short(nxt, self.m.t, float(stats.get("short_s", 2.0)), mech.id)
                last = nxt
        else:
            hit = self.arena.raycast(
                origin,
                [origin[0] + math.cos(yaw) * 45.0, origin[1] + math.sin(yaw) * 45.0, origin[2]],
            )
            if hit is not None:
                path.append(hit[1])
        self.m.add_beam(mech.id, "lightning", path)
        self.m.sound("lightning", origin[0], origin[1], mech)
        return True

    # ------------------------------------------------------------------ projectiles
    def step_projectiles(self, dt):
        t = self.m.t
        for p in self.m.projectiles:
            if p.dead:
                continue
            if p.gravity:
                p.vel[2] -= p.gravity * dt
            p0 = (p.pos[0], p.pos[1], p.pos[2])
            p1 = (p0[0] + p.vel[0] * dt, p0[1] + p.vel[1] * dt, p0[2] + p.vel[2] * dt)
            seg = (p1[0] - p0[0], p1[1] - p0[1], p1[2] - p0[2])
            best_t = 2.0
            hit = None
            # mechs
            for mech in self.m.mechs:
                if not mech.alive or mech.id == p.owner:
                    continue
                if p.owner_kind == "mech" and p.owner_house and mech.house:
                    continue
                if p.owner_kind == "npc" and mech.house and False:
                    continue
                tt = self.arena.ray_cylinder(
                    p0, seg, mech.pos[0], mech.pos[1], mech.pos[2], mech.radius, mech.height
                )
                if tt is not None and tt < best_t:
                    best_t = tt
                    hit = ("mech", mech)
            # npcs
            for npc in self.m.npcs:
                if not npc.alive:
                    continue
                if p.owner_kind == "npc" and npc.id == p.owner:
                    continue
                tt = self.arena.ray_cylinder(
                    p0, seg, npc.pos[0], npc.pos[1], npc.pos[2], npc.radius, npc.height
                )
                if tt is not None and tt < best_t:
                    best_t = tt
                    hit = ("npc", npc)
            # crates
            for crate in self.m.crates:
                if crate["hp"] <= 0:
                    continue
                tt = self.arena.ray_cylinder(
                    p0, seg, crate["pos"][0], crate["pos"][1], crate["pos"][2],
                    float(self.cfg["crates"]["radius"]), float(self.cfg["crates"]["height"]),
                )
                if tt is not None and tt < best_t:
                    best_t = tt
                    hit = ("crate", crate)
            # world
            wh = self.arena.raycast(p0, p1)
            if wh is not None and wh[0] * 1.0 < best_t:
                best_t = wh[0]
                hit = ("world", wh[1])
            if hit is not None and best_t <= 1.0:
                point = (p0[0] + seg[0] * best_t, p0[1] + seg[1] * best_t, p0[2] + seg[2] * best_t)
                self._impact(p, hit[0], hit[1], point, p0)
                p.dead = True
            else:
                p.pos = [p1[0], p1[1], p1[2]]
                if p.pos[2] < self.arena.ground(p.pos[0], p.pos[1]) - 0.1:
                    p.dead = True
                    if p.kind == "mortar":
                        self._explode(p, (p.pos[0], p.pos[1], p.pos[2]))
            p.ttl -= dt
            if p.ttl <= 0.0 and not p.dead:
                p.dead = True
                if p.kind == "mortar":
                    self._explode(p, (p.pos[0], p.pos[1], p.pos[2]))
        if len(self.m.projectiles) > 64:
            self.m.projectiles = [p for p in self.m.projectiles if not p.dead]

    def _impact(self, p, kind, obj, point, origin):
        if p.kind == "mortar":
            # direct hit damage, then splash (the direct victim is excluded from splash)
            owner = self.m.mech_by_id(p.owner)
            if kind == "mech":
                rear = self.is_rear(obj, (p.pos[0], p.pos[1], p.pos[2]))
                self.m.apply_damage(obj, p.damage, "mortar", owner, point, rear=rear)
            elif kind == "npc":
                self.m.apply_damage(obj, p.damage, "mortar", owner, point)
            elif kind == "crate":
                self.m.apply_damage(obj, p.damage, "mortar", owner, point)
            self._explode(p, point, exclude=obj if kind in ("mech", "npc") else None)
            return
        # bullet
        owner = self.m.mech_by_id(p.owner)
        src = owner
        if p.owner_kind == "npc":
            src = self.m.npc_by_id(p.owner)
        if kind == "mech":
            rear = self.is_rear(obj, origin)
            self.m.apply_damage(obj, p.damage, p.kind, src, point, rear=rear)
        elif kind == "npc":
            self.m.apply_damage(obj, p.damage, p.kind, src, point)
        elif kind == "crate":
            self.m.apply_damage(obj, p.damage, p.kind, src, point)
        if p.owner_kind == "npc":
            self.m.sound("npc_gun", p0_xy(p)[0], p0_xy(p)[1], None)

    def _explode(self, p, point, exclude=None):
        owner = self.m.mech_by_id(p.owner)
        radius = p.splash_radius
        if radius <= 0.0:
            return
        self.m.sound("explosion", point[0], point[1], None)
        edge = p.splash_edge
        for mech in list(self.m.mechs):
            if not mech.alive or mech is exclude:
                continue
            if p.owner_house and mech.house and mech.id != p.owner:
                continue  # house bots never attack each other, splash included
            cz = mech.pos[2] + mech.height * 0.5
            d = math.dist(point, (mech.pos[0], mech.pos[1], cz))
            if d > radius:
                continue
            fall = 1.0 - (1.0 - edge) * (d / radius)
            dmg = p.splash * fall
            if mech.id == p.owner:
                dmg *= float(self.cfg["mech"]["self_damage_mult"])
            rear = self.is_rear(mech, point)
            self.m.apply_damage(mech, dmg, "mortar", owner, point, rear=rear)
            if p.knock:
                dx = mech.pos[0] - point[0]
                dy = mech.pos[1] - point[1]
                dd = math.hypot(dx, dy)
                if dd > 1e-6:
                    mech.vel[0] += dx / dd * p.knock * fall
                    mech.vel[1] += dy / dd * p.knock * fall
                mech.vel[2] += p.knock_up * fall
            if p.drain:
                mech.energy = max(0.0, mech.energy - p.drain * fall)
                mech.last_energy_t = self.m.t
            if mech is self.m.player and (owner is None or owner is not mech):
                self.m.player_effect(mech, "concuss", None if owner is None else owner.id)
        for npc in self.m.npcs:
            if not npc.alive:
                continue
            cz = npc.pos[2] + npc.height * 0.5
            d = math.dist(point, (npc.pos[0], npc.pos[1], cz))
            if d <= radius:
                fall = 1.0 - (1.0 - edge) * (d / radius)
                self.m.apply_damage(npc, p.splash * fall, "mortar", owner, point)

    # ------------------------------------------------------------------ aim helper
    def aim_at(self, mech, tx, ty, tz, kind, target=None):
        """Aim [yaw, pitch] for a house bot at world point (tx,ty,tz)."""
        dx = tx - mech.pos[0]
        dy = ty - mech.pos[1]
        horiz = math.hypot(dx, dy)
        yaw = math.atan2(dy, dx)
        if kind == "mortar":
            stats = self.W["mortar"]
            g = float(stats["gravity"])
            v = float(stats["speed"])
            d = max(4.0, horiz)
            ratio = min(0.995, d * g / (v * v))
            pitch = 0.5 * math.asin(ratio)
            dz = tz - (mech.pos[2] + mech.stats["height"] * 0.95 * mech.size)
            pitch += math.atan2(dz, d) * 0.35
            return yaw, pitch
        dz = tz - (mech.pos[2] + mech.muzzle_height)
        pitch = math.atan2(dz, max(1.0, horiz))
        if kind == "gun":
            speed = float(mech.weapons[0].stats.get("speed", 252.0)) if mech.weapons[0] else 252.0
            lead = horiz / speed
            if target is not None:
                tx2 = tx + target.vel[0] * lead
                ty2 = ty + target.vel[1] * lead
                tz2 = tz + target.vel[2] * lead
                dx = tx2 - mech.pos[0]
                dy = ty2 - mech.pos[1]
                horiz2 = math.hypot(dx, dy)
                yaw = math.atan2(dy, dx)
                pitch = math.atan2(tz2 - (mech.pos[2] + mech.muzzle_height), max(1.0, horiz2))
        elif kind == "laser":
            lead = 0.04
            if target is not None:
                tx += target.vel[0] * lead
                ty += target.vel[1] * lead
                tz += target.vel[2] * lead
                dx = tx - mech.pos[0]
                dy = ty - mech.pos[1]
                horiz = math.hypot(dx, dy)
                yaw = math.atan2(dy, dx)
                pitch = math.atan2(tz - (mech.pos[2] + mech.muzzle_height), max(1.0, horiz))
        return yaw, pitch


def p0_xy(p):
    return p.pos[0], p.pos[1]
