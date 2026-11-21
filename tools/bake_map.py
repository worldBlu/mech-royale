#!/usr/bin/env python3
"""Bake the static nav grids into bot/data/nav.bin.

    python tools/bake_map.py
    python tools/bake_map.py --map kit/arena --out bot/data/nav.bin

The match image only ships `bot/`, so the bake output is a normal tracked file: the bot loads it
at startup instead of rasterising 259 prisms and 1560 trees (~800 ms -> ~30 ms). The file is
rejected at load time unless its magic, format version, grid geometry and map fingerprint all
match, so a stale bake can never be used with a different map.

Format (little-endian, 72 byte header, then one zlib stream):

    offset  type      field
    0       8s        magic b"MECHNAV1"
    8       u32       format version (1)
    12      u32       header size (72)
    16      u32       map fingerprint (crc32 of world.json then terrain.i16)
    20      f32       cell size in metres (2.0)
    24      f32       min_x
    28      f32       min_y
    32      f32       max_x
    36      f32       max_y
    40      u32       nx
    44      u32       ny
    48      u32       payload length (compressed bytes)
    52      u32       walk bytes   (n)
    56      u32       sight bytes  (n)
    60      u32       height bytes (2n, int16 centimetres)
    64      u32       comp bytes   (n, uint8, 255 = blocked)
    68      u32       crc32 of the compressed payload

    payload: zlib(walk[n] + sight[n] + height_i16[n] + comp[n]), level 9

The bake is deterministic: two runs produce identical bytes (json parse, rasterisation, component
labels and zlib are all deterministic), which is checked here before the file is written.
"""
import argparse
import hashlib
import os
import sys
import time
import zlib
from array import array

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BOT = os.path.join(ROOT, "bot")
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import world as navworld                        # noqa: E402

DEFAULT_MAP = os.path.join(ROOT, "kit", "arena")
DEFAULT_OUT = navworld.BAKE_FILE


def bake(map_dir):
    """Rasterise the map once and return (world, nav.bin bytes)."""
    t0 = time.perf_counter()
    w = navworld.World(map_dir, use_bake=False)
    t1 = time.perf_counter()
    w.comp = navworld.build_components(w.walk, w.nx, w.ny)
    t2 = time.perf_counter()
    blob = navworld.pack_bake(w)
    t3 = time.perf_counter()
    return w, blob, (t1 - t0) * 1000.0, (t2 - t1) * 1000.0, (t3 - t2) * 1000.0


def describe(w, blob):
    n = w.nx * w.ny
    walk = bytes(w.walk)
    sight = bytes(w.sight)
    comp = bytes(w.comp)
    heights = array("h", (int(round(v * 100.0)) for v in w.height)).tobytes()
    print("map       %s" % os.path.abspath(w.dir))
    print("grid      %d x %d = %d cells of %.1f m, bounds (%.2f, %.2f) .. (%.2f, %.2f)"
          % (w.nx, w.ny, n, navworld.CELL, w.min_x, w.min_y, w.max_x, w.max_y))
    print("walk      %d blocked (%.1f%%), %d walkable"
          % (sum(w.walk), 100.0 * sum(w.walk) / n, n - sum(w.walk)))
    print("sight     %d blocked (%.1f%%)" % (sum(w.sight), 100.0 * sum(w.sight) / n))
    print("height    %.2f .. %.2f m, int16 centimetres" % (min(w.height), max(w.height)))
    labels = max((c for c in comp if c != 255), default=-1) + 1
    print("comp      %d walkable regions, largest %d cells"
          % (labels, comp.count(0) if labels else 0))
    print("fingerprint 0x%08x" % w.map_hash)
    print("payload   walk %d  sight %d  height %d  comp %d  = %d raw bytes"
          % (len(walk), len(sight), len(heights), len(comp),
             len(walk) + len(sight) + len(heights) + len(comp)))
    print("zlib      walk %d  sight %d  height %d  comp %d (level 9, separate arrays)"
          % (len(zlib.compress(walk, 9)), len(zlib.compress(sight, 9)),
             len(zlib.compress(heights, 9)), len(zlib.compress(comp, 9))))
    payload = blob[navworld.BAKE_HEADER:]
    print("nav.bin   %d bytes (header %d + payload %d, crc32 0x%08x)"
          % (len(blob), navworld.BAKE_HEADER, len(payload), zlib.crc32(payload) & 0xFFFFFFFF))
    print("sha256    %s" % hashlib.sha256(blob).hexdigest())


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--map", default=DEFAULT_MAP, help="map directory holding v2/world.json")
    ap.add_argument("--out", default=DEFAULT_OUT, help="output file (default bot/data/nav.bin)")
    ap.add_argument("--skip-verify", action="store_true",
                    help="do not bake a second time to prove the output is deterministic")
    args = ap.parse_args(argv)

    print("bake from %s" % os.path.abspath(args.map))
    w, blob, t_world, t_comp, t_pack = bake(args.map)
    print("timings   rasterise %.0f ms, components %.0f ms, pack %.0f ms"
          % (t_world, t_comp, t_pack))
    describe(w, blob)

    if not args.skip_verify:
        t0 = time.perf_counter()
        _w2, blob2, *_ = bake(args.map)
        same = blob2 == blob
        print("verify    second bake identical: %s (%.0f ms)"
              % ("YES" if same else "NO", (time.perf_counter() - t0) * 1000.0))
        if not same:
            print("FAIL: bake is not deterministic")
            return 1

    out = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    tmp = out + ".tmp"
    with open(tmp, "wb") as f:
        f.write(blob)
    os.replace(tmp, out)
    print("wrote     %s (%d bytes)" % (out, len(blob)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
