"""Authoritative config / hello for the simulator.

The kit ships one recorded stream (``kit/check/stream.jsonl.gz``) whose first line is the
server ``hello``: map description + the full per-match config.  We reuse that hello so the
simulator and the bot always agree on every tunable number (HP, weapon stats, zone phases,
upgrade definitions, ...).  Using the real hello instead of hand-written constants is the
single most important fidelity choice in this sim.
"""
import copy
import gzip
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MAP_DIR = os.path.join(ROOT, "kit", "arena")
STREAM = os.path.join(ROOT, "kit", "check", "stream.jsonl.gz")

_CACHE = {}


def base_hello():
    """The kit's recorded hello (deep-copied so callers can mutate)."""
    if "hello" not in _CACHE:
        with gzip.open(STREAM, "rt", encoding="utf-8") as f:
            _CACHE["hello"] = json.loads(f.readline())
    return copy.deepcopy(_CACHE["hello"])


def base_config():
    return base_hello()["config"]


def make_hello(seed, config=None, slot=0, map_dir=None):
    """Build a hello dict of the same shape the server sends."""
    return {
        "type": "hello",
        "protocol": "mechfight/1",
        "slot": int(slot),
        "seed": int(seed),
        "map": {
            "dir": os.path.abspath(map_dir or MAP_DIR),
            "files": ["v2/world.json", "v2/terrain.i16"],
        },
        "config": copy.deepcopy(config if config is not None else base_config()),
    }


# --------------------------------------------------------------------------- knobs
def set_path(obj, path, value):
    """Set a dotted path inside ``obj`` (creating dicts as needed)."""
    parts = path.split(".")
    cur = obj
    for p in parts[:-1]:
        nxt = cur.get(p)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[p] = nxt
        cur = nxt
    cur[parts[-1]] = value
    return obj


def get_path(obj, path, default=None):
    cur = obj
    for p in path.split("."):
        if not isinstance(cur, dict) or p not in cur:
            return default
        cur = cur[p]
    return cur


def parse_value(text):
    """Parse a CLI knob value the way a human would expect: JSON if possible."""
    try:
        return json.loads(text)
    except Exception:
        return text


def apply_knobs(config, knobs):
    """Apply ``path=value`` overrides to a config dict in place."""
    applied = {}
    for item in knobs or []:
        if "=" not in item:
            raise SystemExit("knob must look like path=value, got %r" % (item,))
        path, raw = item.split("=", 1)
        path = path.strip()
        value = parse_value(raw.strip())
        set_path(config, path, value)
        applied[path] = value
    return applied
