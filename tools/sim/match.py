"""The match loop.

One ``Match`` owns the world for a single simulated game: 16 mechs (the bot under test + 15
house bots), 12 NPC drones, crates, loot, cubes, supply drops, the zone and projectiles.  It
drives the player bot exactly like ``mf.run`` does in the real harness: build a protocol
observation every ``decide_every`` ticks, call ``Bot.act(obs)``, apply the returned action for
the two ticks until the next decision.

Scoring: ``place`` is the number of mechs still alive when the player dies (so the first death
is 16th and the last survivor is 1st); the score is ``16 - place``.  On the 480 s timeout the
survivors are ranked by HP.  Matches stop as soon as the player dies: that is the only death
that affects the score.

Everything is standard library only and deterministic for a given seed.
"""
import math
import random
import time

from . import kitcfg, loader
from .arena import Arena
from .combat import CombatEngine, ang_diff
from .entities import Mech, Npc, Projectile
from .house import HouseAI, LEVEL_COUNTS
from .zone import Zone

TAU = 2.0 * math.pi
DECIDE_EVERY = 2
# House-bot shared "where is the player" track: refresh interval (s) and position noise (m),
# keyed by the highest difficulty still alive.  This is the knob that makes the 15 house bots
# actually hunt, as the track notes say they prefer the weakest player mech.  It is the single
# biggest fidelity lever in the simulator; see docs/sim.md.
HOUSE_PING = {1: (6.0, 70.0), 2: (4.0, 45.0), 3: (2.5, 25.0), 4: (1.5, 12.0)}


def _clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


def _norm_xy(v):
    m = math.hypot(v[0], v[1])
    if m <= 1e-9:
        return 0.0, 0.0
    return v[0] / m, v[1] / m


def _dist2(ax, ay, bx, by):
    return math.hypot(ax - bx, ay - by)


class Match:
    def __init__(self, bot, hello, seed, duration=None, no_fire=False, trace_every=20,
                 collect_events=True, house_vision="pings"):
        self.bot = bot
        self.hello = hello
        self.cfg = hello["config"]
        if house_vision not in ("pings", "vision"):
            raise ValueError("house_vision must be 'pings' or 'vision', got %r" % (house_vision,))
        self.house_vision = house_vision
        # shared sight memory (used only in "vision" mode); pings mode keeps its 6 s leak
        self.house_shared_memory = 3.0
        self.seed = int(seed)
        self.rng = random.Random((self.seed * 6364136223846793005 + 1442695040888963407) & 0xFFFFFFFFFFFF)
        self.duration = float(duration if duration is not None else self.cfg["match"]["duration_s"])
        self.trace_every = int(trace_every)
        self.no_fire = bool(no_fire)
        self.collect_events = bool(collect_events)
        self.map_dir = (hello.get("map") or {}).get("dir") or kitcfg.MAP_DIR

        self.world = loader.get_world(self.map_dir)
        self.arena = Arena(self.map_dir).attach(self.world)
        self.zone = Zone(self.cfg, self.rng)
        self.combat = CombatEngine(self)

        self.t = 0.0
        self.tick = 0
        self.hz = float(self.cfg["match"].get("tick_hz", 20))
        self.dt = 1.0 / self.hz
        self.alive_at_tick_start = 0
        self.bot_errors = 0
        self.act_ms = []
        self.act_over_budget = 0
        self.trace = []
        self.kill_log = []
        self.start_wall = None

        # containers ---------------------------------------------------------
        self.mechs = []
        self.npcs = []
        self.crates = []
        self.cubes = {}
        self.pickups = {}
        self.drops = []
        self.projectiles = []
        self.next_cube = 5000
        self.next_pickup = 8000
        self.next_drop = 20000
        self.drop_calls = sorted(float(x) for x in self.cfg["drops"]["times"])
        self._beams = []
        self.sounds = []
        self.player_events = []
        self.player_sounds = []
        self.player_zone_accum = 0.0
        self.player_place = None
        self.player_offer_seq = 0
        self.house_player_pos = None
        self.house_player_seen_t = -1e9
        self.house_player_visible = False
        self.house_ping_t = 0.0
        # diagnostics proving the house sensing model is actually doing something
        self.house_sight_samples = 0
        self.house_sight_house_total = 0
        self.house_sight_max = 0
        self.house_sight_ids = set()
        self.house_ping_count = 0
        self.house_known_samples = 0
        self.player_seen = {}
        self.player_npc_seen = {}

        self._spawn_mechs()
        self._spawn_world()

    # ================================================================== setup
    def _level_list(self):
        levels = []
        for level, count in enumerate(LEVEL_COUNTS, start=1):
            levels.extend([level] * count)
        self.rng.shuffle(levels)
        return levels

    def _random_spawn(self, taken, min_sep, center=None, radius=None):
        z = self.zone
        cx, cy = center or z.start_center
        r = radius if radius is not None else z.start_radius * float(
            self.cfg["match"]["spawn_zone_fraction"]
        )
        margin = float(self.cfg["match"]["arena_margin"])
        for _ in range(6000):
            a = self.rng.random() * TAU
            d = math.sqrt(self.rng.random()) * r
            x = cx + math.cos(a) * d
            y = cy + math.sin(a) * d
            if not (self.arena.min_x + margin <= x <= self.arena.max_x - margin):
                continue
            if not (self.arena.min_y + margin <= y <= self.arena.max_y - margin):
                continue
            if not self.world.walkable(x, y):
                continue
            gz = self.arena.ground(x, y)
            if self.arena.solid_point(x, y, gz + 1.5):
                continue
            ok = True
            for tx, ty in taken:
                if _dist2(x, y, tx, ty) < min_sep:
                    ok = False
                    break
            if ok:
                return x, y, gz
        # fallback: nearest walkable to the zone centre
        nw = self.world.nearest_walkable(cx, cy, max_r=120.0) or (cx, cy)
        return nw[0], nw[1], self.arena.ground(nw[0], nw[1])

    def _spawn_mechs(self):
        min_sep = float(self.cfg["match"]["spawn_min_separation"])
        taken = []
        spawn = self._random_spawn(taken, min_sep)
        taken.append((spawn[0], spawn[1]))
        self.player = Mech(0, 0, spawn, self.cfg, house=False)
        self.player.aim = [self.rng.random() * TAU - math.pi, 0.0]
        self.player.cmd_aim = list(self.player.aim)
        self.mechs.append(self.player)
        levels = self._level_list()
        levels = levels[: int(self.cfg["match"]["mechs"]) - 1]
        self.house_ais = []
        for i, level in enumerate(levels):
            pos = self._random_spawn(taken, min_sep)
            taken.append((pos[0], pos[1]))
            m = Mech(i + 1, level, pos, self.cfg, house=True)
            m.aim = [self.rng.random() * TAU - math.pi, 0.0]
            m.cmd_aim = list(m.aim)
            # A house bot may start with one secondary.  The recorded stream shows a house
            # mech carrying a laser at t~8 s, which is much earlier than a normal loot route
            # would allow, so the server's house bots clearly do get starting loadouts.
            if self.rng.random() < {1: 0.15, 2: 0.35, 3: 0.60, 4: 0.85}[level]:
                kind = self.rng.choice(["laser", "mortar", "lightning"])
                if m.holding(kind) is None:
                    m.give_weapon(kind, 0.0)
            self.mechs.append(m)
            ai = HouseAI(m, level, self.rng)
            ai.last_pos = (m.pos[0], m.pos[1])
            self.house_ais.append(ai)

    def _ground_point(self, center=None, radius=None, tries=400):
        margin = float(self.cfg["match"]["arena_margin"])
        for _ in range(tries):
            if center is None:
                x = self.rng.uniform(self.arena.min_x + margin, self.arena.max_x - margin)
                y = self.rng.uniform(self.arena.min_y + margin, self.arena.max_y - margin)
            else:
                a = self.rng.random() * TAU
                d = math.sqrt(self.rng.random()) * (radius or 50.0)
                x = center[0] + math.cos(a) * d
                y = center[1] + math.sin(a) * d
            if not self.arena.inside(x, y):
                continue
            if not self.world.walkable(x, y):
                continue
            gz = self.arena.ground(x, y)
            if self.arena.solid_point(x, y, gz + 0.5):
                continue
            return x, y, gz
        nw = self.world.nearest_walkable(
            center[0] if center else self.zone.start_center[0],
            center[1] if center else self.zone.start_center[1],
            max_r=100.0,
        ) or (0.0, 0.0)
        return nw[0], nw[1], self.arena.ground(nw[0], nw[1])

    def _spawn_world(self):
        cfg = self.cfg
        # NPC drones: 12, wandering near a home point.
        for i in range(int(cfg["npc"]["count"])):
            x, y, gz = self._ground_point()
            npc = Npc(100 + i, (x, y, gz), cfg)
            npc.home = [x, y]
            self.npcs.append(npc)
        # Crates: 36.  TODO(roofs): a quarter are on rooftops on the server; the bot never
        # climbs, so the sim puts them all on reachable ground (documented deviation).
        for i in range(int(cfg["crates"]["count"])):
            x, y, gz = self._ground_point()
            self.crates.append({"id": 1000 + i, "pos": [x, y, gz], "hp": float(cfg["crates"]["hp"])})
        # Floor loot: 40 weapon pickups, weights from config (gun weight 0).
        weights = cfg["loot"]["weights"]
        kinds = list(weights.keys())
        wts = [float(weights[k]) for k in kinds]
        for _ in range(int(cfg["loot"]["floor_count"])):
            kind = self.rng.choices(kinds, weights=wts, k=1)[0]
            x, y, gz = self._ground_point()
            self.pickups[self.next_pickup] = {
                "id": self.next_pickup, "kind": kind, "pos": [x, y, gz + 0.4], "ammo": None,
            }
            self.next_pickup += 1

    # ================================================================== helpers
    def mech_by_id(self, mid):
        for m in self.mechs:
            if m.id == mid:
                return m
        return None

    def npc_by_id(self, nid):
        for n in self.npcs:
            if n.id == nid:
                return n
        return None

    def has_mortar(self, mech):
        return mech.holding("mortar") is not None

    def alive_count(self):
        n = 0
        for m in self.mechs:
            if m.alive:
                n += 1
        return n

    def line_clear(self, a, b):
        """2-D line of sight for AI aiming (buildings + tree trunks)."""
        return self.world.has_los(a[0], a[1], b[0], b[1])

    def mech_targets_for_lightning(self, owner):
        """Entities a lightning arc from ``owner`` may pick as its nearest target.

        House bots never attack each other, so a house arc only reaches the player; the
        player's arc can reach every other mech, NPC or crate.
        """
        out = []
        house = bool(getattr(owner, "house", False))
        for m in self.mechs:
            if not m.alive or m is owner:
                continue
            if house and m.house:
                continue
            out.append(m)
        if not house:
            for npc in self.npcs:
                if npc.alive:
                    out.append(npc)
            # TODO(crates): the protocol lets a lightning arc hit crates too; they are plain
            # dicts here and the bot never aims lightning at crates, so leave them out.
        return out

    # ================================================================== vision/sound
    def _vision_numbers(self, mech):
        v = self.cfg["vision"]
        return (
            float(v["range"]) + float(mech.stats.get("vision_bonus", 0.0)),
            float(v["near_range"]) + float(mech.stats.get("near_range_bonus", 0.0)),
            math.radians(float(v["fov_deg"])) * 0.5,
        )

    def can_see_point(self, mech, x, y, z=None):
        if not mech.alive:
            return False
        dx = x - mech.pos[0]
        dy = y - mech.pos[1]
        d = math.hypot(dx, dy)
        vrange, near, half_fov = self._vision_numbers(mech)
        if d > vrange:
            return False
        if d > near:
            ang = abs(ang_diff(math.atan2(dy, dx), mech.aim[0]))
            if ang > half_fov:
                return False
        # 2-D sight grid (buildings + trunks).  Canopy softness and exact 3-D eye lines
        # are approximations; see docs/sim.md.
        if not self.world.has_los(mech.pos[0], mech.pos[1], x, y):
            return False
        return True

    def visible_mech(self, observer, target):
        if not target.alive:
            return False
        return self.can_see_point(observer, target.pos[0], target.pos[1])

    def sound(self, kind, x, y, src=None):
        """Emit a sound; only the player receives observations, so filter here."""
        if not getattr(self, "player", None) or not self.player.alive:
            return
        if src is not None and src is self.player:
            return
        scfg = self.cfg["sound"]
        carry = scfg.get("range", {}).get(kind, 60.0)
        px, py = self.player.pos[0], self.player.pos[1]
        d = math.hypot(x - px, y - py)
        if d > float(carry):
            return
        noise = math.radians(float(scfg.get("bearing_noise_deg", 7.0)))
        hearing = float(self.player.stats.get("hearing", 1.0))
        bearing = math.atan2(y - py, x - px) + self.rng.gauss(0.0, noise * hearing)
        if d < float(scfg.get("near", 60.0)):
            bucket = "near"
        elif d < float(scfg.get("mid", 160.0)):
            bucket = "mid"
        else:
            bucket = "far"
        self.player_sounds.append({"kind": kind, "bearing": round(bearing, 4),
                                   "dist": bucket, "t": self.t})

    def add_beam(self, by, kind, path):
        if by == self.player.id or not self.player.alive:
            return
        seen = False
        for p in path:
            if self.can_see_point(self.player, p[0], p[1]):
                seen = True
                break
        if not seen:
            for i in range(len(path) - 1):
                a, b = path[i], path[i + 1]
                if self.can_see_point(self.player, (a[0] + b[0]) * 0.5, (a[1] + b[1]) * 0.5):
                    seen = True
                    break
        if seen:
            self._beams.append({"by": int(by), "kind": kind, "path": path, "t": self.t})

    def player_event(self, ev):
        if self.collect_events:
            self.player_events.append(ev)

    def player_effect(self, target, effect, by):
        if target is self.player:
            self.player_event({"type": "effect", "effect": effect, "by": by})

    # ================================================================== damage
    def apply_damage(self, target, amount, weapon, source=None, point=None, rear=False):
        if isinstance(target, Mech):
            if not target.alive:
                return
            dmg = float(amount)
            if weapon != "zone":
                dmg *= 1.0 - float(target.stats.get("armor", 0.0))
                if rear:
                    dmg *= float(target.stats.get("rear_mult", 1.5))
            target.hp -= dmg
            target.damage_taken += dmg
            target.last_hit_t = self.t
            if isinstance(source, Mech):
                target._last_cause = weapon
            elif isinstance(source, Npc):
                target._last_cause = weapon + ":npc"
            else:
                target._last_cause = weapon
            if isinstance(source, Mech):
                source.last_hit_t = self.t
                source.damage_dealt += dmg
                source.weapon_damage[weapon] = source.weapon_damage.get(weapon, 0.0) + dmg
                if source is self.player and target is not self.player:
                    source.hits += 1
            if target is self.player:
                ev = {"type": "damaged", "amount": round(dmg, 2), "weapon": weapon}
                if weapon == "zone" or source is None:
                    ev["bearing"] = None
                    ev["by"] = None
                else:
                    sx = source.pos[0] if hasattr(source, "pos") else point[0]
                    sy = source.pos[1] if hasattr(source, "pos") else point[1]
                    ev["bearing"] = round(math.atan2(sy - target.pos[1], sx - target.pos[0]), 4)
                    ev["by"] = int(source.id)
                    if rear:
                        ev["rear"] = True
                self.player_event(ev)
            if isinstance(source, Mech) and source is self.player and target is not self.player:
                hev = {"type": "hit", "target": target.id, "amount": round(dmg, 2),
                       "weapon": weapon}
                if rear:
                    hev["rear"] = True
                self.player_event(hev)
            if target.hp <= 0.0:
                self.kill_mech(target, source if isinstance(source, Mech) else None)
        elif isinstance(target, Npc):
            if not target.alive:
                return
            target.hp -= float(amount)
            if isinstance(source, Mech) and source is self.player:
                source.damage_dealt += float(amount)
                source.weapon_damage[weapon] = source.weapon_damage.get(weapon, 0.0) + float(amount)
                source.hits += 1
                self.player_event({"type": "hit", "target": target.id,
                                   "amount": round(float(amount), 2), "weapon": weapon})
            if target.hp <= 0:
                self.kill_npc(target, source if isinstance(source, Mech) else None)
        elif isinstance(target, dict) and "hp" in target:
            self.damage_crate(target, float(amount), source, weapon)

    def damage_crate(self, crate, amount, source, weapon):
        if crate["hp"] <= 0:
            return
        crate["hp"] -= amount
        if isinstance(source, Mech) and source is self.player:
            self.player_event({"type": "hit", "target": crate["id"],
                               "amount": round(amount, 2), "weapon": weapon})
            source.hits += 1
        if crate["hp"] <= 0:
            crate["hp"] = 0.0
            self.spawn_cube(crate["pos"], 1, spread=1.0)
            if self.rng.random() < float(self.cfg["loot"]["crate_weapon_chance"]):
                kind = self.rng.choices(
                    ["mortar", "laser", "lightning"], weights=[1, 1, 1], k=1
                )[0]
                self.spawn_pickup(kind, crate["pos"], spread=1.0)
            self.sound("crate", crate["pos"][0], crate["pos"][1], None)
            if isinstance(source, Mech) and source is self.player:
                source.crate_kills += 1

    def apply_short(self, target, t, duration, by=None):
        if not isinstance(target, Mech) or not target.alive:
            return
        for w in target.weapons:
            if w is not None and w.kind == "laser":
                w.charge = 0.0
                w.hold = 0.0
                w.short_until = max(w.short_until, t + duration)
        self.player_effect(target, "short", by)

    # ================================================================== death
    def kill_mech(self, mech, killer):
        if not mech.alive:
            return
        mech.alive = False
        place = self.alive_at_tick_start
        mech.place = place
        if mech is self.player:
            self.player_place = place
        self.kill_log.append({"t": round(self.t, 2), "id": mech.id, "place": place,
                              "cause": getattr(mech, "_last_cause", None),
                              "by": None if killer is None else killer.id})
        if killer is not None and killer is self.player:
            killer.kills += 1
            self.player_event({"type": "kill", "target": mech.id})
        if killer is not None:
            killer.last_hit_t = self.t
        self._drop_death_loot(mech)
        self.sound("death", mech.pos[0], mech.pos[1], mech)

    def _drop_death_loot(self, mech):
        extra = int(self.cfg["cubes"]["death_drop_extra"])
        n = int(mech.cubes) + extra
        for _ in range(n):
            self.spawn_cube(mech.pos, 1, spread=2.2)
        for w in mech.weapons:
            if w is not None and w.kind != "gun":
                self.spawn_pickup(w.kind, mech.pos, spread=2.0, ammo=w.ammo)
        mech.cubes = 0
        mech.weapons[1] = None

    def kill_npc(self, npc, killer):
        if not npc.alive:
            return
        npc.alive = False
        for _ in range(int(self.cfg["npc"]["cubes"])):
            self.spawn_cube(npc.pos, 1, spread=1.8)
        if killer is self.player:
            killer.kills += 1
            killer.npc_kills += 1
            self.player_event({"type": "kill", "target": npc.id})

    def spawn_cube(self, pos, count=1, spread=1.0):
        for _ in range(count):
            a = self.rng.random() * TAU
            d = math.sqrt(self.rng.random()) * spread
            x = pos[0] + math.cos(a) * d
            y = pos[1] + math.sin(a) * d
            nw = self.world.nearest_walkable(x, y, max_r=6.0)
            if nw:
                x, y = nw
            gz = self.arena.ground(x, y)
            self.cubes[self.next_cube] = {
                "id": self.next_cube,
                "pos": [x, y, gz + 0.25],
            }
            self.next_cube += 1

    def spawn_pickup(self, kind, pos, spread=1.0, ammo=None):
        a = self.rng.random() * TAU
        d = math.sqrt(self.rng.random()) * spread
        x = pos[0] + math.cos(a) * d
        y = pos[1] + math.sin(a) * d
        nw = self.world.nearest_walkable(x, y, max_r=6.0)
        if nw:
            x, y = nw
        gz = self.arena.ground(x, y)
        pid = self.next_pickup
        self.next_pickup += 1
        self.pickups[pid] = {"id": pid, "kind": kind, "pos": [x, y, gz + 0.4], "ammo": ammo}

    # ================================================================== cubes/upgrades
    def _collect_cubes(self):
        cap = self.cfg["cubes"]["stat_cap"]
        for cid, cube in list(self.cubes.items()):
            cx, cy, cz = cube["pos"]
            for m in self.mechs:
                if not m.alive:
                    continue
                if math.hypot(m.pos[0] - cx, m.pos[1] - cy) <= float(
                    self.cfg["cubes"]["pickup_radius"]
                ) and abs(m.pos[2] - cz) <= 4.0:
                    del self.cubes[cid]
                    m.add_cube()
                    if m is self.player:
                        self.player_event({"type": "pickup", "cubes": 1})
                        self._check_thresholds(m)
                    else:
                        self._check_thresholds_auto(m)
                    break

    def _check_thresholds(self, mech):
        ths = self.cfg["cubes"]["upgrade_thresholds"]
        for th in ths:
            th = int(th)
            if mech.cubes >= th and th not in mech.upg_claimed:
                mech.upg_claimed.add(th)
                mech.pending_offers.append(th)
        if mech is self.player:
            if mech.offer is None and mech.pending_offers:
                self._open_offer(mech)

    def _check_thresholds_auto(self, mech):
        ths = self.cfg["cubes"]["upgrade_thresholds"]
        for th in ths:
            th = int(th)
            if mech.cubes >= th and th not in mech.upg_claimed:
                mech.upg_claimed.add(th)
                avail = mech.available_upgrades()
                if avail:
                    mech.apply_upgrade(self.rng.choice(avail))

    def _open_offer(self, mech):
        avail = mech.available_upgrades()
        if not avail:
            mech.pending_offers = []
            return
        k = min(3, len(avail))
        choices = self.rng.sample(avail, k)
        self.player_offer_seq += 1
        mech.offer = {
            "id": self.player_offer_seq,
            "expires": round(self.t + float(self.cfg["bot"]["offer_timeout_s"]), 2),
            "choices": [
                {"id": u["id"], "name": u.get("name", u["id"]), "desc": u.get("desc", "")}
                for u in choices
            ],
        }
        mech._offer_choices = choices
        mech.pending_offers.pop(0) if mech.pending_offers else None
        mech.offer_deadline = self.t + float(self.cfg["bot"]["offer_timeout_s"])

    def _close_offer(self, mech):
        mech.offer = None
        mech._offer_choices = []
        if mech.pending_offers and mech is self.player:
            self._open_offer(mech)

    def pick_upgrade(self, mech, index):
        offer = mech.offer
        if not offer:
            return
        choices = getattr(mech, "_offer_choices", [])
        try:
            idx = int(index)
        except Exception:
            idx = 0
        if idx < 0 or idx >= len(choices):
            idx = 0
        upg = choices[idx]
        mech.apply_upgrade(upg)
        self.player_event({"type": "upgrade", "id": upg["id"]})
        self._close_offer(mech)

    def _update_offers(self):
        p = self.player
        if p.offer is not None and self.t >= p.offer_deadline:
            choices = getattr(p, "_offer_choices", [])
            if choices:
                # No answer by the deadline: the server applies choice 0.
                p.apply_upgrade(choices[0])
                self.player_event({"type": "upgrade", "id": choices[0]["id"]})
            self._close_offer(p)

    # ================================================================== items
    def _handle_player_items(self, act):
        mech = self.player
        drop_slot = act.get("drop")
        if drop_slot is not None:
            try:
                drop_slot = int(drop_slot)
            except Exception:
                drop_slot = None
            if drop_slot is not None and not (0 <= drop_slot <= 1):
                drop_slot = None
        take_id = act.get("take")
        if take_id is not None:
            self._do_take(mech, take_id, drop_slot)
        elif drop_slot is not None:
            self._do_drop(mech, drop_slot)

    def _do_take(self, mech, take_id, drop_slot=None):
        try:
            take_id = int(take_id)
        except Exception:
            return
        pk = self.pickups.get(take_id)
        if pk is None:
            self.player_event({"type": "take_failed", "id": take_id, "reason": "gone"})
            return
        d = math.hypot(mech.pos[0] - pk["pos"][0], mech.pos[1] - pk["pos"][1])
        dz = abs(mech.pos[2] - pk["pos"][2])
        pr = float(self.cfg["loot"]["pickup_radius"])
        if d > pr or dz > pr:
            self.player_event({"type": "take_failed", "id": take_id, "reason": "range"})
            return
        if mech.holding(pk["kind"]) is not None:
            self.player_event({"type": "take_failed", "id": take_id, "reason": "have"})
            return
        slot = 0 if mech.weapons[0] is None else 1 if mech.weapons[1] is None else None
        if slot is None:
            if drop_slot is None:
                self.player_event({"type": "take_failed", "id": take_id, "reason": "full"})
                return
            old = mech.weapons[drop_slot]
            if old is not None and old.kind == "laser" and old.charge > 0.0:
                self.player_event({"type": "take_failed", "id": take_id, "reason": "busy"})
                return
            slot = drop_slot
            if old is not None:
                dropped = self._drop_weapon(mech, slot)
                if dropped is not None:
                    self.player_event({"type": "dropped", "id": dropped, "kind": old.kind,
                                       "slot": slot})
        del self.pickups[take_id]
        w = mech.give_weapon(pk["kind"], self.t)
        if w is None:
            # shouldn't happen after the checks above
            self.pickups[take_id] = pk
            self.player_event({"type": "take_failed", "id": take_id, "reason": "full"})
            return
        if mech.weapons[w] is not None and pk.get("ammo") is not None:
            mech.weapons[w].ammo = int(pk["ammo"])
        self.sound("pickup", mech.pos[0], mech.pos[1], None)
        self.player_event({"type": "took", "id": take_id, "kind": pk["kind"], "slot": w})

    def _drop_weapon(self, mech, slot):
        w = mech.weapons[slot]
        if w is None:
            return None
        if w.kind == "laser" and w.charge > 0.0:
            return None
        pid = self.next_pickup
        self.next_pickup += 1
        self.pickups[pid] = {
            "id": pid, "kind": w.kind,
            "pos": [mech.pos[0], mech.pos[1], mech.pos[2] + 0.4],
            "ammo": w.ammo if w.kind == "gun" else None,
        }
        mech.weapons[slot] = None
        return pid

    def _do_drop(self, mech, slot):
        w = mech.weapons[slot]
        if w is None:
            self.player_event({"type": "take_failed", "id": 0, "reason": "full"})
            return
        if w.kind == "laser" and w.charge > 0.0:
            return
        pid = self._drop_weapon(mech, slot)
        if pid is not None:
            self.player_event({"type": "dropped", "id": pid, "kind": w.kind, "slot": slot})

    def _auto_pickups_for_houses(self):
        for m in self.mechs:
            if not m.alive or m is self.player:
                continue
            if m.weapons[1] is not None:
                continue
            for pid, pk in list(self.pickups.items()):
                if pk["kind"] == "gun":
                    continue
                if math.hypot(m.pos[0] - pk["pos"][0], m.pos[1] - pk["pos"][1]) <= 5.0:
                    del self.pickups[pid]
                    m.give_weapon(pk["kind"], self.t)
                    break

    # ================================================================== drops
    def _update_drops(self):
        cfg = self.cfg["drops"]
        while self.drop_calls and self.t >= self.drop_calls[0]:
            self.drop_calls.pop(0)
            z = self.zone
            c = z.next_center
            r = max(4.0, z.next_radius * float(cfg.get("spread", 0.7)))
            x, y, gz = self._ground_point(center=c, radius=r * 0.9)
            self.drops.append({
                "id": self.next_drop, "pos": [x, y, gz], "land_t": self.t + float(cfg["fall_s"]),
                "radius": float(cfg["radius"]), "capture_s": float(cfg["capture_s"]),
                "progress": 0.0, "holder": None, "landed": False,
            })
            self.next_drop += 1
        dt = self.dt
        for d in list(self.drops):
            if self.t < d["land_t"]:
                continue
            if not d["landed"]:
                d["landed"] = True
                self.sound("drop", d["pos"][0], d["pos"][1], None)
            inside = []
            for m in self.mechs:
                if not m.alive:
                    continue
                if math.hypot(m.pos[0] - d["pos"][0], m.pos[1] - d["pos"][1]) <= d["radius"]:
                    inside.append(m)
            if not inside:
                d["progress"] = 0.0
                d["holder"] = None
            elif len(inside) == 1:
                m = inside[0]
                if d["holder"] != m.id:
                    d["holder"] = m.id
                    d["progress"] = 0.0
                d["progress"] += dt
                if d["progress"] >= d["capture_s"]:
                    for _ in range(int(cfg["cubes"])):
                        m.add_cube()
                    if float(cfg.get("repair", 1.0)) > 0.0:
                        m.hp = m.max_hp
                        m.energy = m.max_energy
                    if m is self.player:
                        self.player_event({"type": "pickup", "cubes": int(cfg["cubes"])})
                        self._check_thresholds(m)
                    else:
                        self._check_thresholds_auto(m)
                    self.drops.remove(d)
            else:
                # Two or more mechs: the clock freezes (protocol 354-362).
                holders = {m.id for m in inside}
                if d["holder"] is not None and d["holder"] not in holders:
                    d["holder"] = None
                    d["progress"] = 0.0

    # ================================================================== movements
    def can_stand(self, x, y, z):
        if not self.arena.inside(x, y):
            return False
        if not self.world.walkable(x, y):
            return False
        step = float(self.cfg["mech"]["step_up"])
        gz = self.arena.ground(x, y)
        return gz - z <= step + 0.01

    def step_mech(self, mech, dt):
        if not mech.alive:
            return
        st = mech.stats
        # aim slew
        dyaw = ang_diff(mech.cmd_aim[0], mech.aim[0])
        max_yaw = float(st["turn_rate"]) * dt
        mech.aim[0] += _clamp(dyaw, -max_yaw, max_yaw)
        dp = mech.cmd_aim[1] - mech.aim[1]
        max_p = float(st["pitch_rate"]) * dt
        mech.aim[1] += _clamp(dp, -max_p, max_p)
        mech.aim[1] = _clamp(mech.aim[1], float(st["min_pitch"]), float(st["max_pitch"]))

        # movement command (protocol clamps the vector to length 1)
        mx, my = mech.cmd_move[0], mech.cmd_move[1]
        ml = math.hypot(mx, my)
        if ml > 1.0:
            mx, my = mx / ml, my / ml
        charging = any(
            w is not None and w.kind == "laser" and w.charge > 0.0 for w in mech.weapons
        )
        boosting = bool(mech.cmd_boost) and mech.energy > 0.2
        speed = float(st["boost_speed"]) if boosting else float(st["walk_speed"])
        if charging:
            speed *= float(st.get("charge_move_mult", 1.0))
        desired_x = mx * speed
        desired_y = my * speed
        accel = float(st["ground_accel"]) if mech.grounded else float(st["air_accel"])
        dvx = _clamp(desired_x - mech.vel[0], -accel * dt, accel * dt)
        dvy = _clamp(desired_y - mech.vel[1], -accel * dt, accel * dt)
        mech.vel[0] += dvx
        mech.vel[1] += dvy
        nx = mech.pos[0] + mech.vel[0] * dt
        ny = mech.pos[1] + mech.vel[1] * dt
        if self.can_stand(nx, ny, mech.pos[2]):
            mech.pos[0] = nx
            mech.pos[1] = ny
        else:
            moved = False
            if self.can_stand(nx, mech.pos[1], mech.pos[2]):
                mech.pos[0] = nx
                mech.vel[1] = 0.0
                moved = True
            if self.can_stand(mech.pos[0], ny, mech.pos[2]):
                mech.pos[1] = ny
                mech.vel[0] = 0.0
                moved = True
            if not moved:
                mech.vel[0] = 0.0
                mech.vel[1] = 0.0

        gz = self.arena.ground(mech.pos[0], mech.pos[1])
        if mech.grounded:
            if mech.cmd_jump:
                mech.vel[2] = float(st["jump_speed"])
                mech.grounded = False
                mech.pos[2] = max(mech.pos[2], gz) + 0.02
                mech.cmd_jump = False
            elif gz >= mech.pos[2] - 0.6:
                mech.pos[2] = gz
            else:
                mech.grounded = False
                mech.vel[2] = 0.0
        if not mech.grounded:
            mech.vel[2] -= float(st["gravity"]) * dt
            if boosting:
                mech.vel[2] = min(
                    mech.vel[2] + float(st["jet_accel"]) * dt, float(st["max_rise_speed"])
                )
            mech.pos[2] += mech.vel[2] * dt
            if mech.pos[2] <= gz:
                mech.pos[2] = gz
                mech.vel[2] = 0.0
                mech.grounded = True
        mech.cmd_jump = False

        # energy
        if boosting:
            drain = float(st["boost_drain"]) if mech.grounded else float(st["jet_drain"])
            mech.energy = max(0.0, mech.energy - drain * dt)
            mech.last_energy_t = self.t
        elif self.t - mech.last_energy_t >= float(st["energy_regen_delay"]):
            mech.energy = min(
                mech.max_energy,
                mech.energy + float(st["energy_regen"]) * dt,
            )

        # periodic sounds (only used for the player's ears)
        hspeed = math.hypot(mech.vel[0], mech.vel[1])
        if mech.grounded and hspeed > float(self.cfg["sound"]["step_speed"]):
            mech.step_t += dt
            if mech.step_t >= float(self.cfg["sound"]["step_every"]):
                mech.step_t = 0.0
                self.sound("step", mech.pos[0], mech.pos[1], mech)
        else:
            mech.step_t = 0.0
        if boosting:
            every = 0.5
            if mech.grounded:
                if self.t - mech.boost_sound_t >= every:
                    mech.boost_sound_t = self.t
                    self.sound("boost", mech.pos[0], mech.pos[1], mech)
            else:
                if self.t - mech.jet_sound_t >= every:
                    mech.jet_sound_t = self.t
                    self.sound("jet", mech.pos[0], mech.pos[1], mech)

    def push_apart(self):
        mechs = [m for m in self.mechs if m.alive]
        for i in range(len(mechs)):
            a = mechs[i]
            for j in range(i + 1, len(mechs)):
                b = mechs[j]
                dx = b.pos[0] - a.pos[0]
                dy = b.pos[1] - a.pos[1]
                d = math.hypot(dx, dy)
                rr = a.radius + b.radius
                if d >= rr or d < 1e-6:
                    continue
                overlap = (rr - d) * 0.5
                ux, uy = dx / d, dy / d
                ax = a.pos[0] - ux * overlap
                ay = a.pos[1] - uy * overlap
                bx = b.pos[0] + ux * overlap
                by = b.pos[1] + uy * overlap
                if self.can_stand(ax, ay, a.pos[2]):
                    a.pos[0], a.pos[1] = ax, ay
                if self.can_stand(bx, by, b.pos[2]):
                    b.pos[0], b.pos[1] = bx, by

    # ================================================================== weapons
    def update_weapons(self, mech, dt):
        for slot, w in enumerate(mech.weapons):
            if w is None:
                continue
            if w.cooldown > 0.0:
                w.cooldown = max(0.0, w.cooldown - dt)
            if w.reload > 0.0:
                w.reload = max(0.0, w.reload - dt)
                if w.reload <= 1e-9:
                    w.ammo = int(w.stats.get("mag", 0))
            if w.kind == "gun" and w.ammo <= 0 and w.reload <= 0.0:
                w.reload = float(w.stats["reload"])
                self.sound("reload", mech.pos[0], mech.pos[1], mech)
            if (
                not mech.house
                and mech.fire_cmd[slot]
                and w.kind in ("gun", "mortar", "lightning")
            ):
                self.combat.fire(mech, slot)
            if w.kind == "laser" and not mech.house:
                if w.charge > 0.0:
                    if mech.fire_cmd[slot]:
                        w.charge = min(float(w.stats["charge"]), w.charge + dt)
                        if w.charge >= float(w.stats["charge"]):
                            w.hold += dt
                            if w.hold >= float(w.stats["hold_s"]):
                                self.combat.fire_laser(mech, slot)
                    else:
                        if w.charge >= float(w.stats["min_charge"]):
                            self.combat.fire_laser(mech, slot)
                        else:
                            w.charge = 0.0
                elif (
                    mech.fire_cmd[slot]
                    and w.cooldown <= 0.0
                    and self.t >= w.ready_at
                    and self.t >= w.short_until
                ):
                    w.charge = 0.001
                    w.hold = 0.0
                    self.sound("laser_charge", mech.pos[0], mech.pos[1], mech)
        if mech.reload_cmd:
            gun = mech.weapons[0]
            if gun is not None and gun.kind == "gun" and gun.reload <= 0.0 and gun.ammo < int(
                gun.stats.get("mag", 0)
            ):
                gun.reload = float(gun.stats["reload"])
                self.sound("reload", mech.pos[0], mech.pos[1], mech)
            mech.reload_cmd = False

    # ================================================================== actions
    def apply_player_action(self, act):
        m = self.player
        if not isinstance(act, dict):
            act = {}
        move = act.get("move")
        if isinstance(move, (list, tuple)) and len(move) >= 2:
            try:
                m.cmd_move = [float(move[0]), float(move[1])]
            except Exception:
                m.cmd_move = [0.0, 0.0]
        else:
            m.cmd_move = [0.0, 0.0]
        m.cmd_boost = bool(act.get("boost", False))
        if act.get("jump"):
            m.cmd_jump = True
        aim = act.get("aim")
        if isinstance(aim, (list, tuple)) and len(aim) >= 2:
            try:
                m.cmd_aim = [float(aim[0]), float(aim[1])]
            except Exception:
                pass
        fire = act.get("fire")
        if isinstance(fire, (list, tuple)):
            f0 = bool(fire[0]) if len(fire) > 0 else False
            f1 = bool(fire[1]) if len(fire) > 1 else False
            m.fire_cmd = [False if self.no_fire else f0, False if self.no_fire else f1]
        else:
            m.fire_cmd = [False, False]
        m.reload_cmd = bool(act.get("reload", False))
        if act.get("take") is not None or act.get("drop") is not None:
            self._handle_player_items(act)
        pick = act.get("pick")
        if pick is not None and m.offer is not None:
            self.pick_upgrade(m, pick)

    # ================================================================== NPCs
    def update_npcs(self, dt):
        cfg = self.cfg["npc"]
        speed = float(cfg["speed"])
        for npc in self.npcs:
            if not npc.alive:
                continue
            npc.cooldown = max(0.0, npc.cooldown - dt)
            # validate target
            tgt = None
            if npc.target_id is not None:
                tgt = self.mech_by_id(npc.target_id)
                if tgt is not None and (
                    not tgt.alive
                    or math.hypot(tgt.pos[0] - npc.pos[0], tgt.pos[1] - npc.pos[1])
                    > float(cfg["deaggro_range"])
                ):
                    tgt = None
                    npc.target_id = None
            if tgt is None and math.hypot(
                npc.pos[0] - npc.home[0], npc.pos[1] - npc.home[1]
            ) <= float(cfg["leash"]):
                best = None
                bd = float(cfg["aggro_range"])
                for m in self.mechs:
                    if not m.alive:
                        continue
                    d = math.hypot(m.pos[0] - npc.pos[0], m.pos[1] - npc.pos[1])
                    if d < bd and self.line_clear(npc.pos, m.pos):
                        bd = d
                        best = m
                if best is not None:
                    npc.target_id = best.id
                    tgt = best
            if tgt is not None:
                dh = math.hypot(tgt.pos[0] - npc.pos[0], tgt.pos[1] - npc.pos[1])
                if math.hypot(npc.pos[0] - npc.home[0], npc.pos[1] - npc.home[1]) > float(
                    cfg["chase_leash"]
                ):
                    npc.target_id = None
                else:
                    npc.aim = math.atan2(tgt.pos[1] - npc.pos[1], tgt.pos[0] - npc.pos[0])
                    if dh > 25.0:
                        self._npc_move(npc, tgt.pos[0], tgt.pos[1], speed * dt)
                    if (
                        npc.cooldown <= 0.0
                        and dh <= float(cfg["aggro_range"])
                        and self.line_clear(npc.pos, tgt.pos)
                    ):
                        self._npc_fire(npc, tgt)
                        npc.cooldown = float(cfg["cooldown"])
            else:
                if self.t >= npc.wander_t:
                    a = self.rng.random() * TAU
                    d = math.sqrt(self.rng.random()) * float(cfg["leash"])
                    npc.wander = [npc.home[0] + math.cos(a) * d, npc.home[1] + math.sin(a) * d]
                    npc.wander_t = self.t + 4.0 + self.rng.random() * 3.0
                if math.hypot(npc.pos[0] - npc.wander[0], npc.pos[1] - npc.wander[1]) > 2.0:
                    npc.aim = math.atan2(npc.wander[1] - npc.pos[1], npc.wander[0] - npc.pos[0])
                    self._npc_move(npc, npc.wander[0], npc.wander[1], speed * dt)

    def _npc_move(self, npc, tx, ty, step):
        dx = tx - npc.pos[0]
        dy = ty - npc.pos[1]
        d = math.hypot(dx, dy)
        if d < 1e-6:
            return
        nx = npc.pos[0] + dx / d * step
        ny = npc.pos[1] + dy / d * step
        if self.world.walkable(nx, ny) and self.arena.inside(nx, ny):
            npc.pos[0] = nx
            npc.pos[1] = ny
        else:
            if self.world.walkable(nx, npc.pos[1]) and self.arena.inside(nx, npc.pos[1]):
                npc.pos[0] = nx
            elif self.world.walkable(npc.pos[0], ny) and self.arena.inside(npc.pos[0], ny):
                npc.pos[1] = ny
        npc.pos[2] = self.arena.ground(npc.pos[0], npc.pos[1])

    def _npc_fire(self, npc, tgt):
        cfg = self.cfg["npc"]
        yaw = npc.aim + self.rng.gauss(0.0, math.radians(float(cfg["spread_deg"])))
        tx = tgt.pos[0] - npc.pos[0]
        ty = tgt.pos[1] - npc.pos[1]
        tz = tgt.pos[2] + getattr(tgt, "height", 9.0) * 0.45 - (npc.pos[2] + 1.6)
        horiz = max(1.0, math.hypot(tx, ty))
        pitch = math.atan2(tz, horiz) + self.rng.gauss(0.0, math.radians(float(cfg["spread_deg"])))
        speed = float(cfg["projectile_speed"])
        from .combat import CombatEngine

        d = CombatEngine.direction(yaw, pitch)
        origin = [npc.pos[0], npc.pos[1], npc.pos[2] + 1.6]
        p = Projectile(
            self.combat._pid(), "npc", origin,
            [d[0] * speed, d[1] * speed, d[2] * speed],
            npc.id, False, float(cfg["damage"]), self.cfg["projectile"]["npc_radius"],
        )
        p.owner_kind = "npc"
        p.ttl = 2.0
        self.projectiles.append(p)
        self.sound("npc_gun", origin[0], origin[1], None)

    # ================================================================== houses
    def house_think(self):
        if not self.player.alive:
            self.house_player_visible = False
            if self.house_vision == "vision":
                self.house_player_pos = None
            return
        # Union of what the living house bots can see this think.  House aim is where their
        # vision cone points, so this is genuinely LOS/arc gated rather than omniscient.
        sight = 0
        for ai in self.house_ais:
            if ai.mech.alive and self.visible_mech(ai.mech, self.player):
                sight += 1
                self.house_sight_ids.add(ai.mech.id)
        self.house_sight_samples += 1
        self.house_sight_house_total += sight
        if sight > self.house_sight_max:
            self.house_sight_max = sight
        self.house_player_visible = sight > 0
        if sight:
            self.house_player_pos = [self.player.pos[0], self.player.pos[1]]
            self.house_player_seen_t = self.t
        if self.house_vision == "vision":
            # Shared sight only: keep the last exact position for a short team memory, then
            # forget it.  No pings, so cover and breaking line of sight actually work.
            if self.house_player_seen_t < self.t - self.house_shared_memory:
                self.house_player_pos = None
        else:
            if self.house_player_seen_t < self.t - 6.0:
                self.house_player_pos = None
            # The real house bots hunt the weakest player mech.  They clearly do not need a
            # line of sight to know roughly where it is, so model a shared, noisy player
            # track that refreshes faster and more accurately as the house roster gets harder.
            if self.player.alive and not self.house_player_visible and self.t >= self.house_ping_t:
                levels = [ai.level for ai in self.house_ais if ai.mech.alive]
                lvl = max(levels) if levels else 1
                interval, noise = HOUSE_PING[lvl]
                self.house_ping_t = self.t + interval
                a = self.rng.random() * TAU
                d = math.sqrt(self.rng.random()) * noise
                self.house_player_pos = [
                    self.player.pos[0] + math.cos(a) * d,
                    self.player.pos[1] + math.sin(a) * d,
                ]
                self.house_player_seen_t = self.t
                self.house_ping_count += 1
        if self.house_player_pos is not None:
            self.house_known_samples += 1
        for ai in self.house_ais:
            if ai.mech.alive:
                ai.think(self)

    # ================================================================== offers/step
    def zone_step(self, dt):
        z = self.zone
        dps = z.dps
        for m in self.mechs:
            if not m.alive:
                continue
            if z.in_zone(m.pos[0], m.pos[1]):
                continue
            dmg = dps * dt
            m.hp -= dmg
            m.damage_taken += dmg
            m.zone_damage += dmg
            m.time_outside_zone += dt
            m.last_hit_t = self.t
            m._last_cause = "zone"
            if m is self.player:
                self.player_zone_accum += dmg
            if m.hp <= 0.0:
                self.kill_mech(m, None)

    def _regen(self, dt):
        for m in self.mechs:
            if not m.alive or m.hp >= m.max_hp:
                continue
            if not self.zone.in_zone(m.pos[0], m.pos[1]):
                continue
            if self.t - m.last_hit_t < float(m.stats["regen_delay"]):
                continue
            m.hp = min(m.max_hp, m.hp + float(m.stats["regen_per_s"]) * dt)

    # ================================================================== main loop
    def _build_obs(self):
        me = self.player
        t = self.t
        self.zone.update(t)
        selfobs = me.as_self_obs()
        selfobs["in_zone"] = bool(self.zone.in_zone(me.pos[0], me.pos[1]))
        if self.player_zone_accum > 0.0:
            self.player_event({"type": "damaged",
                               "amount": round(self.player_zone_accum, 2),
                               "weapon": "zone", "bearing": None, "by": None})
            self.player_zone_accum = 0.0
        obs = {
            "type": "obs",
            "tick": self.tick,
            "t": round(t, 3),
            "alive": self.alive_count(),
            "self": selfobs,
            "mechs": [],
            "projectiles": [],
            "crates": [],
            "cubes": [],
            "pickups": [],
            "sounds": [],
            "beams": [],
            "events": [],
            "zone": self.zone.state(),
            "offer": me.offer,
            "drops": [],
        }
        memory = float(self.cfg["vision"]["memory_s"])
        # mechs + npcs, with server-side tracking memory
        for m in self.mechs:
            if m is me or not m.alive:
                continue
            if self.visible_mech(me, m):
                self.player_seen[m.id] = t
                obs["mechs"].append(m.as_other_obs())
            elif m.id in self.player_seen and t - self.player_seen[m.id] <= memory:
                obs["mechs"].append(m.as_other_obs())
        for npc in self.npcs:
            if not npc.alive:
                continue
            if self.visible_mech(me, npc):
                self.player_npc_seen[npc.id] = t
                obs["mechs"].append(npc.as_obs())
            elif npc.id in self.player_npc_seen and t - self.player_npc_seen[npc.id] <= memory:
                obs["mechs"].append(npc.as_obs())
        # projectiles
        for p in self.projectiles:
            if p.dead or p.owner == me.id:
                continue
            if self.can_see_point(me, p.pos[0], p.pos[1]):
                obs["projectiles"].append(p.as_obs())
        # crates / cubes / pickups
        for c in self.crates:
            if c["hp"] > 0 and self.can_see_point(me, c["pos"][0], c["pos"][1]):
                obs["crates"].append({"id": c["id"], "pos": [round(v, 3) for v in c["pos"]],
                                      "hp": round(c["hp"], 2)})
        for cube in self.cubes.values():
            if self.can_see_point(me, cube["pos"][0], cube["pos"][1]):
                obs["cubes"].append({"id": cube["id"],
                                     "pos": [round(v, 3) for v in cube["pos"]]})
        for pk in self.pickups.values():
            if self.can_see_point(me, pk["pos"][0], pk["pos"][1]):
                obs["pickups"].append({"id": pk["id"], "kind": pk["kind"],
                                       "pos": [round(v, 3) for v in pk["pos"]]})
        # sounds / beams (events since the last observation)
        for s in self.player_sounds:
            obs["sounds"].append({"kind": s["kind"], "bearing": round(s["bearing"], 4),
                                  "dist": s["dist"], "age": round(t - s["t"], 2)})
        self.player_sounds = []
        for b in self._beams:
            obs["beams"].append({"by": b["by"], "kind": b["kind"],
                                 "path": [[round(v, 3) for v in p] for p in b["path"]],
                                 "age": round(t - b["t"], 2)})
        self._beams = []
        obs["events"] = self.player_events
        self.player_events = []
        # supply drops are always visible
        for d in self.drops:
            obs["drops"].append({
                "id": d["id"],
                "pos": [round(v, 3) for v in d["pos"]],
                "radius": round(d["radius"], 2),
                "lands_in": round(max(0.0, d["land_t"] - t), 2),
                "capture_s": d["capture_s"],
                "progress": round(d["progress"], 2),
                "holder": d["holder"],
            })
        return obs

    def step(self, dt):
        self.alive_at_tick_start = self.alive_count()
        self.zone.update(self.t)
        self.zone_step(dt)
        for m in self.mechs:
            if m.alive:
                self.step_mech(m, dt)
        for m in self.mechs:
            if m.alive:
                self.update_weapons(m, dt)
        if self.tick % 4 == 0:
            self.house_think()
        self.update_npcs(dt)
        self.combat.step_projectiles(dt)
        self._collect_cubes()
        self._auto_pickups_for_houses()
        self._update_drops()
        self.push_apart()
        self._regen(dt)
        self._update_offers()
        self.projectiles = [p for p in self.projectiles if not p.dead]
        self.t += dt
        self.tick += 1
        if self.tick % self.trace_every == 0:
            self.trace.append([
                round(self.t, 1), round(self.player.hp, 1), self.alive_count(),
                round(self.player.pos[0], 1), round(self.player.pos[1], 1),
                round(self.player.energy, 1), int(self.player.cubes),
            ])

    def run(self):
        self.start_wall = time.time()
        decide = int(self.cfg["match"].get("decide_every", DECIDE_EVERY))
        self.player_seen = {}
        self.player_npc_seen = {}
        end_reason = "time"
        while True:
            if self.tick % decide == 0:
                self.zone.update(self.t)
                obs = self._build_obs()
                t0 = time.perf_counter()
                try:
                    act = self.bot.act(obs)
                except Exception:
                    self.bot_errors += 1
                    act = {}
                ms = (time.perf_counter() - t0) * 1000.0
                self.act_ms.append(ms)
                if ms > float(self.cfg["bot"]["tick_budget_ms"]):
                    self.act_over_budget += 1
                self.apply_player_action(act)
            self.step(self.dt)
            if not self.player.alive:
                end_reason = "death"
                break
            if self.alive_count() <= 1:
                end_reason = "last"
                break
            if self.t >= self.duration:
                end_reason = "time"
                break
        return self.finish(end_reason)

    # ================================================================== finish
    def finish(self, end_reason):
        me = self.player
        if me.alive:
            alive = [m for m in self.mechs if m.alive]
            alive.sort(key=lambda m: (m.hp, m.max_hp), reverse=True)
            place = alive.index(me) + 1 if me in alive else len(alive) + 1
        else:
            place = int(self.player_place or self.alive_count() + 1)
        score = 16.0 - place
        act = self.act_ms
        mean_ms = sum(act) / len(act) if act else 0.0
        return {
            "seed": self.seed,
            "place": int(place),
            "score": float(score),
            "end": end_reason,
            "t": round(self.t, 2),
            "duration_s": round(self.t, 2),
            "alive_end": self.alive_count(),
            "hp_end": round(me.hp, 1) if me.alive else 0.0,
            "kills": int(me.kills),
            "npc_kills": int(me.npc_kills),
            "crate_kills": int(me.crate_kills),
            "damage_dealt": round(me.damage_dealt, 1),
            "damage_taken": round(me.damage_taken, 1),
            "weapon_damage": {k: round(v, 1) for k, v in me.weapon_damage.items()},
            "zone_damage": round(me.zone_damage, 1),
            "time_outside_zone": round(me.time_outside_zone, 2),
            "cubes": int(me.cubes),
            "upgrades": list(me.upgrades),
            "shots": int(me.shots),
            "hits": int(me.hits),
            "decisions": len(act),
            "act_ms_mean": round(mean_ms, 3),
            "act_ms_max": round(max(act), 3) if act else 0.0,
            "over_budget": int(self.act_over_budget),
            "bot_errors": int(self.bot_errors),
            "bot_fails": int(getattr(self.bot, "fails", 0)),
            "projectiles_live": len(self.projectiles),
            "npcs_alive": sum(1 for n in self.npcs if n.alive),
            "houses_alive": sum(1 for m in self.mechs if m.house and m.alive),
            "house_vision": self.house_vision,
            "house_sight_avg": round(
                self.house_sight_house_total / self.house_sight_samples, 3
            ) if self.house_sight_samples else 0.0,
            "house_sight_max": int(self.house_sight_max),
            "house_sight_ever": len(self.house_sight_ids),
            "house_pings": int(self.house_ping_count),
            "house_known_frac": round(
                self.house_known_samples / self.house_sight_samples, 3
            ) if self.house_sight_samples else 0.0,
            "mode": getattr(getattr(self.bot, "policy", None), "mode", None),
            "trace": self.trace,
            "kill_log": self.kill_log,
            "wall_s": round(time.time() - self.start_wall, 2) if self.start_wall else None,
        }
