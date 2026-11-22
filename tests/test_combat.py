"""Synthetic combat tests: aim solutions, projectile fly-out, fire gating.

Run directly:

    C:\\Users\\yuan_\\.dsh\\...\\python.exe tests\\test_combat.py

Only the standard library is used.  The stub world makes line of sight explicit so the
tests exercise Combat's geometry, not the map bake.
"""
import math
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from bot.combat import Combat, ang_diff  # noqa: E402

# ---------------------------------------------------------------- protocol numbers
CFG = {
    "mech": {
        "radius": 2.5, "height": 9.0, "eye_height": 8.0, "muzzle_height": 7.0,
        "turn_rate": 4.0, "pitch_rate": 3.0, "min_pitch": -0.7, "max_pitch": 1.3,
        "hp": 1400.0, "energy": 100.0, "regen": 18.0, "regen_delay": 5.0,
        "regen_per_s": 45.0, "walk": 11.7, "boost": 19.5, "gravity": 22.0,
    },
    "vision": {"range": 200.0, "fov": 120.0, "near": 45.0, "memory_s": 5.0},
    "weapons": {
        "gun": {"damage": 45.0, "cooldown": 0.24, "speed": 252.0, "range": 230.0,
                "spread_deg": 0.6, "mag": 12, "reload": 2.4},
        "mortar": {"damage": 200.0, "splash": 160.0, "splash_radius": 7.0,
                   "cooldown": 2.6, "speed": 72.0, "gravity": 22.0, "range": 235.0},
        "laser": {"damage": 315.0, "charge": 0.75, "min_charge": 0.15,
                  "min_frac": 0.35, "hold_s": 2.0, "cooldown": 1.65, "range": 280.0},
        "lightning": {"damage": 60.0, "range": 45.0, "cone_deg": 30.0,
                      "energy": 3.0, "cooldown": 0.25, "chain": 2},
    },
    "projectile": {"gravity": 22.0},
}

MECH_STATS = dict(CFG["mech"])


class StubWorld:
    """Line of sight is just a flag; `terrain` is flat unless a test sets it."""

    def __init__(self, los=True, terrain=None):
        self.los = los
        self.terrain = terrain
        self.calls = 0

    def has_los(self, x0, y0, x1, y1):
        self.calls += 1
        return self.los

    def terrain_z(self, x, y):
        return self.terrain


class Track:
    def __init__(self, tid=7, kind="mech", pos=(100.0, 0.0, 0.0), vel=(0.0, 0.0, 0.0),
                 seen=0.0, cubes=0):
        self.id = tid
        self.kind = kind
        self.x, self.y, self.z = pos
        self.vx, self.vy, self.vz = vel
        self.seen = seen
        self.cubes = cubes


def gun_w(ammo=12, cooldown=0.0, reload_left=0.0, **stats):
    st = dict(CFG["weapons"]["gun"])
    st.update(stats)
    return {"kind": "gun", "ammo": ammo, "cooldown": cooldown,
            "reload": reload_left, "stats": st}


def laser_w(charge=0.0, cooldown=0.0, **stats):
    st = dict(CFG["weapons"]["laser"])
    st.update(stats)
    return {"kind": "laser", "charge": charge, "cooldown": cooldown, "stats": st}


def mortar_w(cooldown=0.0, **stats):
    st = dict(CFG["weapons"]["mortar"])
    st.update(stats)
    return {"kind": "mortar", "cooldown": cooldown, "stats": st}


def lightning_w(cooldown=0.0, **stats):
    st = dict(CFG["weapons"]["lightning"])
    st.update(stats)
    return {"kind": "lightning", "cooldown": cooldown, "stats": st}


def make_me(weapons, aim=(0.0, 0.0), pos=(0.0, 0.0, 0.0), vel=(0.0, 0.0, 0.0),
            energy=100.0):
    return {
        "pos": list(pos), "vel": list(vel), "aim": list(aim), "energy": energy,
        "hp": 1400.0, "stats": dict(MECH_STATS),
        "weapons": list(weapons),
    }


def make_obs(me, targets=(), projectiles=()):
    return {"t": 0.0, "self": me, "mechs": list(targets), "projectiles": list(projectiles),
            "beams": [], "events": []}


def proj(kind="gun", pos=(-50.0, 0.0, 4.5), vel=(252.0, 0.0, 0.0), pid=1):
    return {"id": pid, "kind": kind, "pos": list(pos), "vel": list(vel)}


def target_obs(tr):
    return {"id": tr.id, "kind": tr.kind, "pos": [tr.x, tr.y, tr.z],
            "vel": [tr.vx, tr.vy, tr.vz], "hp": 1400.0}


def dir_from(aim):
    yaw, pitch = aim
    cp = math.cos(pitch)
    return [cp * math.cos(yaw), cp * math.sin(yaw), math.sin(pitch)]


def target_center(tr):
    if tr.kind == "npc":
        return (tr.x, tr.y, tr.z + 1.75)
    scale = max(1.0, 1.0 + 0.05 * float(tr.cubes or 0.0))
    return (tr.x, tr.y, tr.z + 4.5 * scale)


def muzzle_height(me, kind):
    st = me.get("stats") or {}
    h = float(st.get("muzzle_height", 7.0))
    return h + (2.5 if kind == "mortar" else 0.0)


def simulate_min_dist(me, tr, kind, aim, dt=0.001, tmax=8.0):
    """Fly the projectile from the 3 m shot origin; return closest approach to the body.

    The target moves linearly from its track position/velocity, and the mortar shell is
    ballistic with the protocol gravity.  A laser is hitscan: closest approach of the ray.
    """
    v = float(CFG["weapons"][kind]["speed"]) if kind != "laser" else None
    d = dir_from(aim)
    muz = muzzle_height(me, kind)
    origin = (me["pos"][0] + d[0] * 3.0,
              me["pos"][1] + d[1] * 3.0,
              me["pos"][2] + muz + d[2] * 3.0)
    c0 = target_center(tr)
    if kind == "laser":
        # perpendicular distance from the current centre to the ray
        rel = (c0[0] - origin[0], c0[1] - origin[1], c0[2] - origin[2])
        t = rel[0] * d[0] + rel[1] * d[1] + rel[2] * d[2]
        if t < 0:
            return math.sqrt(rel[0] ** 2 + rel[1] ** 2 + rel[2] ** 2)
        p = (origin[0] + d[0] * t, origin[1] + d[1] * t, origin[2] + d[2] * t)
        return math.sqrt((p[0] - c0[0]) ** 2 + (p[1] - c0[1]) ** 2 + (p[2] - c0[2]) ** 2)
    best = float("inf")
    t = 0.0
    while t <= tmax:
        if kind == "mortar":
            p = (origin[0] + d[0] * v * t, origin[1] + d[1] * v * t,
                 origin[2] + d[2] * v * t - 0.5 * 22.0 * t * t)
        else:
            p = (origin[0] + d[0] * v * t, origin[1] + d[1] * v * t,
                 origin[2] + d[2] * v * t)
        tp = (c0[0] + tr.vx * t, c0[1] + tr.vy * t, c0[2] + tr.vz * t)
        best = min(best, math.sqrt((p[0] - tp[0]) ** 2 + (p[1] - tp[1]) ** 2
                                   + (p[2] - tp[2]) ** 2))
        if kind == "mortar" and p[2] < tp[2] - 60.0:
            break
        t += dt
    return best


def aligned_me(combat, tr, kind, weapons=None, d=None):
    """Torso already on the solved aim: this is the steady-state fire case."""
    if d is None:
        d = math.hypot(tr.x, tr.y)
    me = make_me(weapons if weapons is not None else [gun_w(), None])
    aim = combat.aim_for(me, tr, kind, d)
    me["aim"] = list(aim)
    return me, aim


class AimSolutionTests(unittest.TestCase):
    def setUp(self):
        self.world = StubWorld(True)
        self.combat = Combat(self.world, CFG)

    def test_stationary_mech_gun(self):
        for d in (20.0, 80.0, 150.0, 220.0):
            tr = Track(pos=(d, 0.0, 0.0), seen=0.0)
            me, aim = aligned_me(self.combat, tr, "gun")
            self.assertAlmostEqual(aim[0], 0.0, places=9, msg="gun yaw d=%s" % d)
            miss = simulate_min_dist(me, tr, "gun", aim)
            self.assertLess(miss, 2.5, "gun stationary miss %.2f m at %.0f m" % (miss, d))

    def test_stationary_mech_laser(self):
        for d in (20.0, 80.0, 150.0, 220.0):
            tr = Track(pos=(d, 0.0, 0.0), seen=0.0)
            me, aim = aligned_me(self.combat, tr, "laser", weapons=[None, laser_w()])
            self.assertAlmostEqual(aim[0], 0.0, places=9)
            miss = simulate_min_dist(me, tr, "laser", aim)
            self.assertLess(miss, 2.5, "laser miss %.2f m at %.0f" % (miss, d))

    def test_stationary_mech_mortar(self):
        for d in (20.0, 80.0, 150.0, 220.0):
            tr = Track(pos=(d, 0.0, 0.0), seen=0.0)
            me, aim = aligned_me(self.combat, tr, "mortar", weapons=[None, mortar_w()])
            self.assertAlmostEqual(aim[0], 0.0, places=9)
            miss = simulate_min_dist(me, tr, "mortar", aim)
            self.assertLess(miss, 2.5, "mortar miss %.2f m at %.0f" % (miss, d))

    def test_mortar_pitch_is_low_angle(self):
        tr = Track(pos=(150.0, 0.0, 0.0))
        me, aim = aligned_me(self.combat, tr, "mortar", weapons=[None, mortar_w()])
        self.assertGreater(aim[1], 0.1)
        self.assertLess(aim[1], math.pi / 4.0 + 0.01)

    def test_crossing_targets_gunnery(self):
        speed = 19.5
        for kind in ("gun", "laser", "mortar"):
            for d in (20.0, 80.0, 150.0, 220.0):
                tr = Track(pos=(d, 0.0, 0.0), vel=(0.0, speed, 0.0), seen=0.0)
                weapons = [gun_w(), None]
                if kind == "laser":
                    weapons = [None, laser_w()]
                elif kind == "mortar":
                    weapons = [None, mortar_w()]
                me, aim = aligned_me(self.combat, tr, kind, weapons=weapons)
                miss = simulate_min_dist(me, tr, kind, aim)
                self.assertLess(miss, 2.5,
                                "%s crossing miss %.2f m at %.0f" % (kind, miss, d))
                if kind != "laser":
                    self.assertGreater(aim[0], 0.0)

    def test_lightning_direction_and_range(self):
        for d in (10.0, 20.0, 44.0):
            tr = Track(pos=(d, 0.0, 0.0))
            me, aim = aligned_me(self.combat, tr, "lightning", weapons=[None, lightning_w()])
            self.assertAlmostEqual(aim[0], 0.0, places=9)
            self.assertTrue(self.combat.can_hit("lightning", d, True))
        for d in (46.0, 80.0, 150.0, 220.0):
            tr = Track(pos=(d, 0.0, 0.0))
            me, aim = aligned_me(self.combat, tr, "lightning", weapons=[None, lightning_w()])
            self.assertAlmostEqual(aim[0], 0.0, places=9)
            self.assertFalse(self.combat.can_hit("lightning", d, True))

    def test_lightning_cone(self):
        # 30 degree cone: 0.45 rad off-axis is inside, 0.60 rad is outside
        for off, expect in ((0.45, True), (0.60, False)):
            tr2 = Track(pos=(30.0 * math.cos(off), 30.0 * math.sin(off), 0.0))
            me2 = make_me([None, lightning_w()], aim=(0.0, 0.0))
            obs = make_obs(me2, [target_obs(tr2)])
            out = self.combat.decide(obs, tr2, 0.0)
            fire = out.get("fire") or [False, False]
            self.assertEqual(fire[1], expect,
                             "lightning cone off=%.2f fire=%s" % (off, fire))

    def test_mortar_high_and_low_ground(self):
        for z in (20.0, -20.0):
            tr = Track(pos=(120.0, 0.0, z))
            me, aim = aligned_me(self.combat, tr, "mortar", weapons=[None, mortar_w()])
            miss = simulate_min_dist(me, tr, "mortar", aim)
            self.assertLess(miss, 2.5, "mortar z=%.0f miss %.2f" % (z, miss))

    def test_stale_track_extrapolation(self):
        tr = Track(pos=(100.0, 0.0, 0.0), vel=(0.0, 10.0, 0.0), seen=0.0)
        me = make_me([gun_w(), None])
        cur = self.combat.aim_for(me, tr, "gun", 100.0, now=0.4)
        tr_now = Track(pos=(100.0, 4.0, 0.0), vel=(0.0, 10.0, 0.0), seen=0.4)
        fresh = self.combat.aim_for(me, tr_now, "gun", 100.0)
        # extrapolating to now=0.4 must agree with a track observed at y=4.0
        self.assertAlmostEqual(cur[0], fresh[0], places=3)
        self.assertAlmostEqual(cur[1], fresh[1], places=3)

    def test_cubes_raise_target_centre(self):
        flat = Track(pos=(100.0, 0.0, 0.0), cubes=0)
        fat = Track(pos=(100.0, 0.0, 0.0), cubes=10)
        me = make_me([gun_w(), None])
        a = self.combat.aim_for(me, flat, "gun", 100.0)
        b = self.combat.aim_for(me, fat, "gun", 100.0)
        self.assertGreater(b[1], a[1])

    def test_falling_target_lead_clamped_to_ground(self):
        # A falling mech lands instead of sinking through the map, so a linear vz lead
        # must not aim below the terrain at the predicted point.
        blind = Combat(StubWorld(True), CFG)                 # no terrain information
        grounded = Combat(StubWorld(True, terrain=0.0), CFG)
        tr = Track(pos=(100.0, 0.0, 2.0), vel=(0.0, 0.0, -16.0))
        me_gun = make_me([gun_w(), None])
        gun_flat = blind.aim_for(me_gun, tr, "gun", 100.0)
        gun_ground = grounded.aim_for(me_gun, tr, "gun", 100.0)
        self.assertLess(gun_flat[1], -0.05)
        self.assertGreater(gun_ground[1], gun_flat[1] + 0.02)
        me_mortar = make_me([None, mortar_w()])
        m_flat = blind.aim_for(me_mortar, tr, "mortar", 100.0)
        m_ground = grounded.aim_for(me_mortar, tr, "mortar", 100.0)
        self.assertLess(m_flat[1], 0.0)
        self.assertGreater(m_ground[1], 0.1)


class FireGatingTests(unittest.TestCase):
    def setUp(self):
        self.world = StubWorld(True)
        self.combat = Combat(self.world, CFG)

    def test_gun_fires_when_aligned_and_withheld_when_slewing(self):
        tr = Track(pos=(80.0, 0.0, 0.0))
        me, aim = aligned_me(self.combat, tr, "gun")
        obs = make_obs(me, [target_obs(tr)])
        out = self.combat.decide(obs, tr, 0.0)
        self.assertEqual(out.get("fire"), [True, False])
        self.assertEqual(len(out.get("fire")), 2)
        self.assertIsNone(out.get("reload"))

        me2, _ = aligned_me(self.combat, tr, "gun")
        me2["aim"] = [aim[0] + 0.5, aim[1]]
        out2 = self.combat.decide(make_obs(me2, [target_obs(tr)]), tr, 0.0)
        self.assertFalse(any(out2.get("fire") or []))

    def test_gun_withholds_on_pitch_error(self):
        tr = Track(pos=(80.0, 0.0, 0.0))
        me, aim = aligned_me(self.combat, tr, "gun")
        me["aim"] = [aim[0], aim[1] + 0.30]
        out = self.combat.decide(make_obs(me, [target_obs(tr)]), tr, 0.0)
        self.assertFalse(any(out.get("fire") or []))

    def test_gun_withheld_when_pitch_outside_limits(self):
        # target almost straight up and close: desired pitch > max_pitch 1.3 -> never fire
        tr = Track(pos=(20.0, 0.0, 100.0))
        me, aim = aligned_me(self.combat, tr, "gun")
        self.assertGreater(aim[1], MECH_STATS["max_pitch"])
        out = self.combat.decide(make_obs(me, [target_obs(tr)]), tr, 0.0)
        self.assertFalse(any(out.get("fire") or []))

    def test_visible_mech_overrides_grid_los(self):
        tr = Track(pos=(80.0, 0.0, 0.0))
        world = StubWorld(False)
        combat = Combat(world, CFG)
        me, _ = aligned_me(combat, tr, "gun")
        out = combat.decide(make_obs(me, [target_obs(tr)]), tr, 0.0)
        self.assertEqual(out.get("fire"), [True, False])
        # same geometry but not in obs.mechs and grid says blocked -> no shot
        out2 = combat.decide(make_obs(me, []), tr, 0.0)
        self.assertFalse(any(out2.get("fire") or [False, False]))

    def test_laser_starts_charging_and_holds(self):
        tr = Track(pos=(100.0, 0.0, 0.0))
        for charge in (0.0, 0.10, 0.20, 0.50):
            me, _ = aligned_me(self.combat, tr, "laser", weapons=[None, laser_w(charge=charge)])
            out = self.combat.decide(make_obs(me, [target_obs(tr)]), tr, 0.0)
            self.assertEqual(out.get("fire"), [False, True],
                             "laser charge %.2f must hold" % charge)

    def test_laser_release_needs_min_charge_and_alignment(self):
        tr = Track(pos=(100.0, 0.0, 0.0))
        # aligned but below the release threshold -> still holding
        me, _ = aligned_me(self.combat, tr, "laser", weapons=[None, laser_w(charge=0.71)])
        out = self.combat.decide(make_obs(me, [target_obs(tr)]), tr, 0.0)
        self.assertEqual(out.get("fire"), [False, True])
        # at the release threshold -> the release is the shot
        me, _ = aligned_me(self.combat, tr, "laser", weapons=[None, laser_w(charge=0.72)])
        out = self.combat.decide(make_obs(me, [target_obs(tr)]), tr, 0.0)
        self.assertEqual(out.get("fire"), [False, False])
        # threshold reached but misaligned -> keep holding, do not throw the shot away
        me, aim = aligned_me(self.combat, tr, "laser", weapons=[None, laser_w(charge=0.72)])
        me["aim"] = [aim[0] + 0.5, aim[1]]
        out = self.combat.decide(make_obs(me, [target_obs(tr)]), tr, 0.0)
        self.assertEqual(out.get("fire"), [False, True])

    def test_laser_cooldown_prevents_release(self):
        tr = Track(pos=(100.0, 0.0, 0.0))
        me, _ = aligned_me(self.combat, tr, "laser",
                           weapons=[None, laser_w(charge=0.72, cooldown=1.0)])
        out = self.combat.decide(make_obs(me, [target_obs(tr)]), tr, 0.0)
        self.assertEqual(out.get("fire"), [False, False])
        self.assertFalse(self.combat.charging)

    def test_mortar_withheld_when_misaligned(self):
        tr = Track(pos=(120.0, 0.0, 0.0))
        me, aim = aligned_me(self.combat, tr, "mortar", weapons=[None, mortar_w()])
        out = self.combat.decide(make_obs(me, []), tr, 0.0)   # no LOS -> mortar pick
        self.assertEqual(out.get("fire"), [False, True])
        me["aim"] = [aim[0] + 0.5, aim[1]]
        out2 = self.combat.decide(make_obs(me, []), tr, 0.0)
        self.assertFalse(any(out2.get("fire") or []))

    def test_gun_reload_path(self):
        tr = Track(pos=(80.0, 0.0, 0.0))
        me, _ = aligned_me(self.combat, tr, "gun", weapons=[gun_w(ammo=0, reload_left=0.0)])
        out = self.combat.decide(make_obs(me, [target_obs(tr)]), tr, 0.0)
        self.assertTrue(out.get("reload"))
        self.assertEqual(out.get("fire", [False, False]), [False, False])
        # already reloading: no repeated reload request
        me, _ = aligned_me(self.combat, tr, "gun", weapons=[gun_w(ammo=0, reload_left=1.0)])
        out = self.combat.decide(make_obs(me, [target_obs(tr)]), tr, 0.0)
        self.assertNotIn("reload", out)


class PickTests(unittest.TestCase):
    def setUp(self):
        self.world = StubWorld(True)
        self.combat = Combat(self.world, CFG)

    def test_laser_beats_gun(self):
        tr = Track(pos=(80.0, 0.0, 0.0))
        me = make_me([gun_w(), laser_w()], aim=(0.0, 0.0))
        self.assertEqual(self.combat.pick(me, 80.0, True, tr), 1)

    def test_lightning_beats_laser_close(self):
        tr = Track(pos=(30.0, 0.0, 0.0))
        me = make_me([gun_w(), lightning_w()], aim=(0.0, 0.0))
        self.assertEqual(self.combat.pick(me, 30.0, True, tr), 1)

    def test_mortar_used_when_no_los(self):
        tr = Track(pos=(120.0, 0.0, 0.0))
        me = make_me([gun_w(), mortar_w()], aim=(0.0, 0.0))
        self.assertEqual(self.combat.pick(me, 120.0, False, tr), 1)

    def test_use_mortar_knob_defaults_on(self):
        self.assertTrue(self.combat.use_mortar)

    def test_use_mortar_off_falls_through(self):
        cfg = dict(CFG)
        cfg["combat"] = {"use_mortar": 0}
        combat = Combat(StubWorld(True), cfg)
        self.assertFalse(combat.use_mortar)
        tr = Track(pos=(120.0, 0.0, 0.0))
        me = make_me([gun_w(), mortar_w()], aim=(0.0, 0.0))
        # no LOS: gun cannot fire and the mortar is disabled by the knob
        self.assertIsNone(combat.pick(me, 120.0, False, tr))
        # with LOS the gun still works (the mortar is the only thing switched off)
        self.assertEqual(combat.pick(me, 120.0, True, tr), 0)

    def test_gun_preferred_when_los(self):
        tr = Track(pos=(120.0, 0.0, 0.0))
        me = make_me([gun_w(), mortar_w()], aim=(0.0, 0.0))
        self.assertEqual(self.combat.pick(me, 120.0, True, tr), 0)


class DodgeTests(unittest.TestCase):
    def setUp(self):
        self.combat = Combat(StubWorld(True), CFG)
        self.me = make_me([gun_w(), None])          # at the origin, aim east

    def test_near_miss_flagged_with_miss_and_time(self):
        obs = make_obs(self.me, projectiles=[proj(pos=(-50.0, 2.0, 4.5))])
        threats = self.combat.threat_scan(obs, 0.0)
        self.assertEqual(len(threats), 1)
        th = threats[0]
        self.assertEqual(th["kind"], "gun")
        self.assertAlmostEqual(th["miss"], 2.0, places=6)
        self.assertAlmostEqual(th["t"], 50.0 / 252.0, places=6)
        self.assertGreater(th["urgency"], 0.0)
        self.assertLessEqual(th["urgency"], 1.0)

    def test_far_miss_not_flagged(self):
        obs = make_obs(self.me, projectiles=[proj(pos=(-50.0, 20.0, 4.5))])
        self.assertEqual(self.combat.threat_scan(obs, 0.0), [])

    def test_past_projectile_ignored(self):
        # 2 m lateral, but already behind us and flying away: no dodge needed
        obs = make_obs(self.me, projectiles=[proj(pos=(5.0, 2.0, 4.5))])
        self.assertEqual(self.combat.threat_scan(obs, 0.0), [])

    def test_zero_velocity_nan_and_junk_do_not_raise(self):
        junk = [proj(pos=(-5.0, 0.0, 4.5), vel=(0.0, 0.0, 0.0)),
                {"id": 2, "kind": "gun", "pos": (float("nan"), 0.0, 4.5),
                 "vel": (252.0, 0.0, 0.0)},
                {"id": 3, "kind": "gun", "pos": (-5.0, 0.0, 4.5),
                 "vel": (float("inf"), 0.0, 0.0)},
                "not a projectile", {}]
        obs = make_obs(self.me, projectiles=junk)
        self.assertEqual(self.combat.threat_scan(obs, 0.0), [])
        self.assertEqual(self.combat.dodge_vector(obs, self.me), (0.0, 0.0, 0.0))
        self.assertEqual(self.combat.threat_scan({}, 0.0), [])
        self.assertEqual(self.combat.dodge_vector(None, None), (0.0, 0.0, 0.0))

    def test_mortar_uses_splash_radius(self):
        # 5.5 m off the line: outside the gun's 4 m body radius, inside the 7 m splash
        gun_obs = make_obs(self.me, projectiles=[proj("gun", (-50.0, 5.5, 4.5))])
        mortar_obs = make_obs(self.me, projectiles=[proj("mortar", (-50.0, 5.5, 4.5))])
        self.assertEqual(self.combat.threat_scan(gun_obs, 0.0), [])
        self.assertEqual(len(self.combat.threat_scan(mortar_obs, 0.0)), 1)

    def test_dodge_is_perpendicular_and_forward(self):
        # Path runs east at y=-2; our velocity is north, which is also away from the path.
        me = make_me([gun_w(), None], aim=(0.0, 0.0), vel=(0.0, 8.0, 0.0))
        obs = make_obs(me, projectiles=[proj(pos=(-40.0, -2.0, 4.5))])
        dx, dy, urgency = self.combat.dodge_vector(obs, me)
        self.assertAlmostEqual(dx, 0.0, places=6)
        self.assertGreater(dy, 0.99)
        self.assertTrue(0.0 <= urgency <= 1.0)
        self.assertGreater(urgency, 0.0)

    def test_dodge_leans_away_when_forward_is_parallel(self):
        # Forward is along the path, so neither perpendicular side is more forward; the
        # dodge should keep to the side we are already on (north of a path at y=-3).
        me = make_me([gun_w(), None], aim=(0.0, 0.0), vel=(8.0, 0.0, 0.0))
        obs = make_obs(me, projectiles=[proj(pos=(-40.0, -3.0, 4.5))])
        dx, dy, _ = self.combat.dodge_vector(obs, me)
        self.assertAlmostEqual(dx, 0.0, places=6)
        self.assertGreater(dy, 0.5)

    def test_no_threat_returns_zero(self):
        self.assertEqual(self.combat.dodge_vector(make_obs(self.me), self.me),
                         (0.0, 0.0, 0.0))

    def test_threats_sorted_most_urgent_first(self):
        obs = make_obs(self.me, projectiles=[proj(pos=(-50.0, 2.0, 4.5), pid=1),
                                             proj(pos=(-200.0, 0.5, 4.5), pid=2)])
        threats = self.combat.threat_scan(obs, 0.0)
        self.assertEqual(len(threats), 2)
        self.assertEqual(threats[0]["id"], 2)      # tighter miss beats the nearer-in-time one
        self.assertLessEqual(threats[0]["urgency"], 1.0)


class CanHitTests(unittest.TestCase):
    def setUp(self):
        self.combat = Combat(StubWorld(True), CFG)

    def test_ranges(self):
        self.assertTrue(self.combat.can_hit("gun", 229.0, True))
        self.assertFalse(self.combat.can_hit("gun", 250.0, True))
        self.assertTrue(self.combat.can_hit("laser", 279.0, True))
        self.assertFalse(self.combat.can_hit("laser", 281.0, True))
        self.assertFalse(self.combat.can_hit("laser", 100.0, False))
        self.assertTrue(self.combat.can_hit("lightning", 44.0, True))
        self.assertFalse(self.combat.can_hit("lightning", 46.0, True))
        self.assertFalse(self.combat.can_hit("mortar", 19.0, True))
        self.assertTrue(self.combat.can_hit("mortar", 30.0, True))
        self.assertFalse(self.combat.can_hit("mortar", 235.0, True))


if __name__ == "__main__":
    unittest.main(verbosity=2)

