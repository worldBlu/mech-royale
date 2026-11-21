"""In-process loading of the bot under test and of the kit's example bot.

Both bots are ordinary ``Bot(hello)`` constructors from the kit's point of view.  Two extra
pieces make in-process evaluation cheap and safe:

* the map bake (~0.8 s) is cached across matches; ``main.World`` is replaced with a factory
  returning the shared read-only ``World``;
* ``example_bot.py`` calls ``mf.run(Bot)`` at import time, so ``mechfight.run`` is patched to
  a no-op and ``mf.draw`` is cleared after every decision (the buffer would otherwise grow).
"""
import importlib.util
import os
import sys

from . import kitcfg

SIM_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(SIM_DIR))
BOT_DIR = os.path.join(ROOT, "bot")
KIT_PY = os.path.join(ROOT, "kit", "python")

_world_mod = None
_world_cache = {}


def _bot_world_module():
    global _world_mod
    if _world_mod is None:
        path = os.path.join(BOT_DIR, "world.py")
        spec = importlib.util.spec_from_file_location("mechsim_bot_world", path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["mechsim_bot_world"] = mod
        spec.loader.exec_module(mod)
        _world_mod = mod
    return _world_mod


def get_world(map_dir=None):
    """Shared read-only baked map (cached by absolute path)."""
    key = os.path.abspath(map_dir or kitcfg.MAP_DIR)
    world = _world_cache.get(key)
    if world is None:
        world = _bot_world_module().World(key)
        _world_cache[key] = world
    return world


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def load_main(cache_world=True):
    """Load bot/main.py in-process.  Returns ``(BotClass, module)``."""
    if BOT_DIR not in sys.path:
        sys.path.insert(0, BOT_DIR)
    mod = _load_module("mechsim_bot_main", os.path.join(BOT_DIR, "main.py"))
    if cache_world:
        try:
            # main.py does ``from world import World``; replace its module-level binding so
            # Bot.__init__ uses the shared instance instead of baking the map again.
            mod.World = lambda d: get_world(d)
        except Exception:
            pass
    return mod.Bot, mod


def load_example():
    """Load kit/python/example_bot.py without letting it start its own stdin loop."""
    if KIT_PY not in sys.path:
        sys.path.insert(0, KIT_PY)
    import mechfight as mf

    if not getattr(mf, "_mechsim_patched", False):
        mf.run = lambda cls=None, *a, **k: None
        mf._mechsim_patched = True
    mod = _load_module("mechsim_example_bot", os.path.join(KIT_PY, "example_bot.py"))
    return mod.Bot, mf


class ExampleBotWrapper:
    """Thin wrapper so example-bot decisions clear the debug draw buffer."""

    def __init__(self, bot, draw):
        self.bot = bot
        self.draw = draw

    def act(self, obs):
        act = self.bot.act(obs)
        try:
            self.draw.clear()
        except Exception:
            pass
        return act


_EXAMPLE_CLASS = None


def make_example_bot_factory():
    global _EXAMPLE_CLASS
    if _EXAMPLE_CLASS is None:
        _EXAMPLE_CLASS, mf = load_example()
    else:
        mf = sys.modules.get("mechfight")
    draw = mf.draw if mf is not None else None

    def factory(hello):
        return ExampleBotWrapper(_EXAMPLE_CLASS(hello), draw)

    return factory
