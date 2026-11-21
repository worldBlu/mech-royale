"""Mech Royale bot: entry point.

`mf.run` owns the stdin/stdout protocol (hello -> ready, one observation at a time, newest
answer wins) and calls `Bot.act` for every observation; everything here is construction and
a safety net, so a bug in one tick costs a tick rather than the match.
"""
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import mf
from combat import Combat
from nav import Navigator
from perceive import Perception
from policy import Policy
from world import World

FALLBACK_MAP = os.path.normpath(os.path.join(HERE, "..", "kit", "arena"))


def map_dir(hello):
    d = (hello.get("map") or {}).get("dir") or "/arena"
    if os.path.isfile(os.path.join(d, "v2", "world.json")):
        return d
    return FALLBACK_MAP


class Bot:
    name = "mech-royale"

    def __init__(self, hello):
        cfg = hello.get("config") or {}
        self.cfg = cfg
        self.world = World(map_dir(hello))
        self.nav = Navigator(self.world)
        self.perc = Perception(self.world, cfg)
        self.combat = Combat(self.world, cfg)
        self.policy = Policy(self.world, self.nav, self.perc, self.combat, cfg)
        self.fails = 0

    def act(self, obs):
        try:
            return self.policy.act(obs)
        except Exception:
            self.fails += 1
            traceback.print_exc(file=sys.stderr)
            me = obs.get("self") or {}
            aim = me.get("aim") or [0.0, 0.0]
            return {"move": [0.0, 0.0], "boost": False, "aim": [float(aim[0]), float(aim[1])]}


def main():
    mf.run(Bot)


if __name__ == "__main__":
    main()
