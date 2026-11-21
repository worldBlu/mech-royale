"""Deterministic, standard-library-only Mech Royale match simulator.

The simulator is an approximation of the server described by ``kit/protocol.md`` and
``kit/check/stream.jsonl.gz``.  It exists to rank bot variants quickly: it is not a
bit-exact clone of the server.  See ``docs/sim.md`` for the explicit fidelity list.

Modules
-------
kitcfg   authoritative ``hello``/``config`` loading (from the kit's recorded stream)
arena    map geometry, walkability, 3-D ray casts against prisms/terrain
zone     zone phase timeline and next-circle generation
entities mechs, weapons, NPCs, projectiles
combat   weapon firing, hit resolution, damage/splash/chains
house    scripted house bots (easy/medium/hard/expert)
match    the match loop: obs construction, actions, physics, scoring
loader   in-process bot loading (main bot and the kit example bot)
"""

__version__ = "1.0"
