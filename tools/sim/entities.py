"""Dynamic entities: mechs, weapons, drones, projectiles.

Only the fields the simulator and the protocol observation need are stored.  Numbers come
from the real hello config passed in by ``Match``.
"""
import math

HP_PER_CUBE = 0.04
DAMAGE_PER_CUBE = 0.04
STAT_CAP = 10
SIZE_PER_CUBE = 0.05


def apply_op(value, op, amount):
    return value * amount if op == "mul" else value + amount


class Weapon:
    __slots__ = (
        "kind", "stats", "cooldown", "ammo", "reload", "charge", "hold",
        "ready_at", "short_until",
    )

    def __init__(self, kind, stats, equipped_at=0.0, equip_s=None):
        self.kind = kind
        self.stats = dict(stats)
        self.cooldown = 0.0
        self.ammo = int(self.stats.get("mag", 0))
        self.reload = 0.0
        self.charge = 0.0
        self.hold = 0.0
        if equip_s is None:
            equip_s = 0.0
        self.ready_at = equipped_at + max(float(equip_s), float(self.stats.get("cooldown", 0.0)))
        self.short_until = -1e9

    def as_obs(self):
        d = {"kind": self.kind, "cooldown": round(self.cooldown, 3), "stats": dict(self.stats)}
        if self.kind == "gun":
            d["ammo"] = int(self.ammo)
            d["reload"] = round(self.reload, 3)
        if self.kind == "laser":
            d["charge"] = round(self.charge, 3)
        return d


class Mech:
    """A player or house mech.  ``level`` 0 = the bot under test."""

    def __init__(self, mid, level, pos, cfg, house=False):
        self.id = int(mid)
        self.level = int(level)
        self.house = bool(house)
        self.cfg = cfg
        self.base_stats = dict(cfg["mech"])
        self.stats = dict(cfg["mech"])
        self.pos = [float(pos[0]), float(pos[1]), float(pos[2])]
        self.vel = [0.0, 0.0, 0.0]
        self.aim = [0.0, 0.0]
        self.cmd_aim = [0.0, 0.0]
        self.cmd_move = [0.0, 0.0]
        self.cmd_boost = False
        self.cmd_jump = False
        self.cubes = 0
        self.upgrades = []
        self.upg_levels = {}
        self.upg_claimed = set()
        self.pending_offers = []
        self.offer = None
        self.offer_deadline = 0.0
        self.wmods = {}
        self.alive = True
        self.grounded = True
        self.size = 1.0
        self.hp = float(self.stats["hp"])
        self.max_hp = float(self.stats["hp"])
        self.energy = float(self.stats["energy"])
        self.max_energy = float(self.stats["energy"])
        self.weapons = [Weapon("gun", cfg["weapons"]["gun"]), None]
        self.fire_cmd = [False, False]
        self.reload_cmd = False
        self.last_hit_t = -1e9
        self.last_energy_t = -1e9
        self.step_t = 0.0
        self.jet_sound_t = -1e9
        self.boost_sound_t = -1e9
        # stats used by reports
        self.damage_dealt = 0.0
        self.damage_taken = 0.0
        self.kills = 0
        self.shots = 0
        self.hits = 0
        self.npc_kills = 0
        self.crate_kills = 0
        self.time_outside_zone = 0.0
        self.zone_damage = 0.0
        self.weapon_damage = {}
        self.refresh()

    # ------------------------------------------------------------------ stats
    def refresh(self):
        """Recompute cube/upgrade-derived numbers after an upgrade or cube pickup."""
        cap = self.cfg["cubes"]["stat_cap"]
        n = min(self.cubes, cap)
        self.size = 1.0 + SIZE_PER_CUBE * self.cubes
        self.max_hp = self.stats["hp"] * (1.0 + HP_PER_CUBE * n)
        self.max_energy = self.stats["energy"]
        if self.hp > self.max_hp:
            self.hp = self.max_hp
        if self.energy > self.max_energy:
            self.energy = self.max_energy

    @property
    def radius(self):
        return self.stats["radius"] * self.size

    @property
    def height(self):
        return self.stats["height"] * self.size

    @property
    def eye_height(self):
        return self.stats["eye_height"] * self.size

    @property
    def muzzle_height(self):
        return self.stats["muzzle_height"] * self.size

    def damage_mult(self):
        cap = self.cfg["cubes"]["stat_cap"]
        return 1.0 + DAMAGE_PER_CUBE * min(self.cubes, cap)

    def add_cube(self):
        old = self.max_hp
        self.cubes += 1
        self.refresh()
        self.hp = min(self.max_hp, self.hp + (self.max_hp - old))

    def holding(self, kind):
        for i, w in enumerate(self.weapons):
            if w is not None and w.kind == kind:
                return i
        return None

    def give_weapon(self, kind, t, from_base=True):
        """Equip into an empty slot; returns the slot or None."""
        w = Weapon(kind, self.cfg["weapons"][kind], equipped_at=t, equip_s=self.cfg["loot"]["equip_s"])
        # Apply the mech's existing weapon upgrades for this kind.
        for stat, op, amount in self.wmods.get(kind, ()):
            w.stats[stat] = apply_op(w.stats.get(stat, 0.0), op, amount)
        if kind == "gun":
            w.ammo = int(w.stats.get("mag", 0))
        for slot in (0, 1):
            if self.weapons[slot] is None:
                self.weapons[slot] = w
                return slot
        return None

    def weapon_stats(self, slot):
        w = self.weapons[slot]
        return None if w is None else self.cfg["weapons"][w.kind]

    # ------------------------------------------------------------------ upgrades
    def apply_upgrade(self, upg):
        uid = upg["id"]
        lvl = self.upg_levels.get(uid, 0)
        if lvl >= int(upg.get("max", 1)):
            return False
        self.upg_levels[uid] = lvl + 1
        self.upgrades.append(uid)
        for eff in upg.get("effects") or []:
            path = eff["path"]
            op = "mul" if "mul" in eff else "add"
            amount = float(eff.get(op, 0.0))
            parts = path.split(".")
            if parts[0] == "mech" and len(parts) >= 2:
                stat = parts[1]
                old_max = self.max_hp
                if stat in self.stats:
                    self.stats[stat] = apply_op(self.stats[stat], op, amount)
                if stat == "hp":
                    self.refresh()
                    self.hp = min(self.max_hp, self.hp + (self.max_hp - old_max))
                elif stat == "energy":
                    old_e = self.max_energy
                    self.refresh()
                    self.energy = min(self.max_energy, self.energy + (self.max_energy - old_e))
            elif parts[0] == "weapons" and len(parts) >= 3:
                kind, stat = parts[1], parts[2]
                self.wmods.setdefault(kind, []).append((stat, op, amount))
                for w in self.weapons:
                    if w is not None and w.kind == kind:
                        w.stats[stat] = apply_op(w.stats.get(stat, 0.0), op, amount)
                        if stat == "mag" and kind == "gun":
                            w.ammo = min(int(w.stats["mag"]), w.ammo + int(amount))
        return True

    def available_upgrades(self):
        out = []
        for upg in self.cfg["upgrades"]:
            if self.upg_levels.get(upg["id"], 0) >= int(upg.get("max", 1)):
                continue
            req = upg.get("requires")
            if req and self.holding(req) is None:
                continue
            out.append(upg)
        return out

    # ------------------------------------------------------------------ obs
    def stats_obs(self):
        d = dict(self.stats)
        d["radius"] = round(self.radius, 3)
        d["height"] = round(self.height, 3)
        d["eye_height"] = round(self.eye_height, 3)
        d["muzzle_height"] = round(self.muzzle_height, 3)
        d["hp"] = round(self.max_hp, 2)   # stats.hp is the max-hp base in the server obs
        return d

    def as_self_obs(self):
        return {
            "id": self.id,
            "pos": [round(v, 3) for v in self.pos],
            "vel": [round(v, 3) for v in self.vel],
            "aim": [round(self.aim[0], 4), round(self.aim[1], 4)],
            "hp": round(self.hp, 2),
            "max_hp": round(self.max_hp, 2),
            "energy": round(self.energy, 2),
            "max_energy": round(self.max_energy, 2),
            "grounded": bool(self.grounded),
            "cubes": int(self.cubes),
            "upgrades": list(self.upgrades),
            "in_zone": False,  # filled by Match (needs the zone)
            "weapons": [None if w is None else w.as_obs() for w in self.weapons],
            "stats": self.stats_obs(),
        }

    def as_other_obs(self):
        return {
            "id": self.id,
            "kind": "mech",
            "pos": [round(v, 3) for v in self.pos],
            "vel": [round(v, 3) for v in self.vel],
            "aim": [round(self.aim[0], 4), round(self.aim[1], 4)],
            "hp": round(self.hp, 2),
            "max_hp": round(self.max_hp, 2),
            "cubes": int(self.cubes),
            "weapons": [w.kind for w in self.weapons if w is not None],
            "charging": any(
                w is not None and w.kind == "laser" and w.charge > 0.0 for w in self.weapons
            ),
        }


class Npc:
    __slots__ = (
        "id", "pos", "vel", "aim", "hp", "max_hp", "home", "alive", "target_id",
        "cooldown", "wander", "wander_t", "kind", "hit_t",
    )

    def __init__(self, nid, pos, cfg):
        self.id = int(nid)
        self.pos = [float(pos[0]), float(pos[1]), float(pos[2])]
        self.vel = [0.0, 0.0, 0.0]
        self.aim = 0.0
        self.hp = float(cfg["npc"]["hp"])
        self.max_hp = float(cfg["npc"]["hp"])
        self.home = [self.pos[0], self.pos[1]]
        self.alive = True
        self.target_id = None
        self.cooldown = 0.0
        self.wander = [self.pos[0], self.pos[1]]
        self.wander_t = 0.0
        self.kind = "npc"
        self.hit_t = -1e9

    @property
    def radius(self):
        return 1.8

    @property
    def height(self):
        return 3.5

    def as_obs(self):
        return {
            "id": self.id,
            "kind": "npc",
            "pos": [round(v, 3) for v in self.pos],
            "vel": [round(v, 3) for v in self.vel],
            "aim": [round(self.aim, 4), 0.0],
            "hp": round(self.hp, 2),
            "max_hp": round(self.max_hp, 2),
            "cubes": 0,
            "weapons": [],
            "charging": False,
        }


class Projectile:
    __slots__ = (
        "id", "kind", "pos", "vel", "owner", "owner_house", "owner_kind", "damage",
        "radius", "ttl", "gravity", "splash", "splash_radius", "splash_edge", "knock",
        "knock_up", "drain", "dead",
    )

    def __init__(self, pid, kind, pos, vel, owner, owner_house, damage, radius=0.3):
        self.id = int(pid)
        self.kind = kind
        self.pos = [float(pos[0]), float(pos[1]), float(pos[2])]
        self.vel = [float(vel[0]), float(vel[1]), float(vel[2])]
        self.owner = owner          # Mech id or Npc id
        self.owner_house = bool(owner_house)
        self.owner_kind = "mech"
        self.damage = float(damage)
        self.radius = float(radius)
        self.ttl = 5.0
        self.gravity = 0.0
        self.splash = 0.0
        self.splash_radius = 0.0
        self.splash_edge = 0.3
        self.knock = 0.0
        self.knock_up = 0.0
        self.drain = 0.0
        self.dead = False

    def as_obs(self):
        return {
            "id": self.id,
            "kind": self.kind,
            "pos": [round(v, 3) for v in self.pos],
            "vel": [round(v, 3) for v in self.vel],
        }
