"""Set a build variant's knob defaults in bot/policy.py.

Usage: python tools/set_knobs.py standoff.gun=180 standoff.laser=220 finish_hp=0.35
"""
import io
import json
import re
import sys

P = "bot/policy.py"


def main(argv):
    src = io.open(P, encoding="utf-8").read()
    start = src.index("KNOBS = {")
    end = src.index("\n}\n", start) + 2
    block = src[start:end]
    data = {}
    # keep the dict literal as source of truth: values may be dicts, ints, floats
    for name, val in json.loads(
        json.dumps({})  # placeholder, real parsing below
    ).items():
        data[name] = val
    # parse the literal with ast
    import ast

    tree = ast.parse(block)
    node = tree.body[0].value
    for k, v in zip(node.keys, node.values):
        data[k.value] = ast.literal_eval(v)

    for item in argv:
        key, val = item.split("=", 1)
        if "." in key:
            outer, inner = key.split(".", 1)
            data.setdefault(outer, {})[inner] = json.loads(val)
        else:
            data[key] = json.loads(val)

    lines = ["KNOBS = {"]
    for k, v in data.items():
        lines.append("    %s: %s," % (json.dumps(k), json.dumps(v)))
    lines.append("}")
    new_block = "\n".join(lines) + "\n"
    src = src[:start] + new_block + src[end:]
    io.open(P, "w", encoding="utf-8", newline="\n").write(src)
    print("knobs:", json.dumps({k: data[k] for k in list(data)[:3]}, sort_keys=True), "...")
    print("set:", ", ".join(argv))


if __name__ == "__main__":
    main(sys.argv[1:])
