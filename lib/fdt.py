#!/usr/bin/env python3
"""A flattened device tree, read from the blob itself.

    fdt.py summary FILE [--json]   what the tree describes
    fdt.py diff A B                what differs, node by node; status 1 when they differ
    fdt.py id FILE                 a digest of the hardware it describes (/chosen left out)
    fdt.py check FILE              status 0 for a well-formed blob
    fdt.py kind FILE               tree or overlay; status 2 when FILE is no blob
    fdt.py carried WANT GOT        what of WANT did not arrive in GOT; status 1 when something did not
    fdt.py prop FILE PATH NAME     one property's value; status 1 when it is not there
    fdt.py image-size IMAGE        the memory a Linux Image header says the kernel occupies
"""
from __future__ import annotations

import hashlib
import json
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from treeinfo import c  # noqa: E402

MAGIC = 0xD00DFEED
HEADER = 40
BEGIN_NODE, END_NODE, PROP, NOP, END = 1, 2, 3, 4, 9
SEEDS = {("/chosen", "kaslr-seed"), ("/chosen", "rng-seed")}
IMAGE_MAGICS = (b"ARM\x64", b"RSC\x05")
SHOWN_CELLS = 16
MAX_DEPTH = 64


class FdtError(ValueError):
    pass


class Node:
    def __init__(self, name, parent=None):
        self.name = name
        self.parent = parent
        self.props = {}
        self.children = []

    @property
    def path(self):
        if self.parent is None:
            return "/"
        up = self.parent.path
        return up + self.name if up == "/" else up + "/" + self.name

    def child(self, name):
        bare = "@" not in name
        for n in self.children:
            if n.name == name or (bare and n.name.split("@", 1)[0] == name):
                return n
        return None

    def walk(self):
        yield self
        for n in self.children:
            yield from n.walk()

    def text(self, prop):
        v = self.props.get(prop)
        return v.split(b"\0", 1)[0].decode("utf-8", "backslashreplace") if v else None

    def strings(self, prop):
        v = self.props.get(prop)
        return [s.decode("utf-8", "backslashreplace") for s in v.rstrip(b"\0").split(b"\0")] if v else []

    def cells(self, prop):
        v = self.props.get(prop, b"")
        n = len(v) // 4
        return list(struct.unpack(">%dI" % n, v[:n * 4]))

    def cell(self, prop, default=None):
        v = self.cells(prop)
        return v[0] if v else default

    def enabled(self):
        return self.text("status") in (None, "okay", "ok")


class Tree:
    def __init__(self, root, reserve, header):
        self.root = root
        self.reserve = reserve
        self.header = header

    def node(self, path):
        n = self.root
        for part in filter(None, path.split("/")):
            n = n.child(part)
            if n is None:
                return None
        return n


def parse(data):
    if len(data) < HEADER:
        raise FdtError("shorter than a device tree header")
    (magic, total, off_struct, off_strings, off_rsv, version, last, boot_cpu,
     size_strings, size_struct) = struct.unpack(">10I", data[:HEADER])
    if magic != MAGIC:
        raise FdtError("no device tree magic")
    if version < 17 or last > 17:
        raise FdtError("format version %d (readable as %d) is not version 17" % (version, last))
    if total > len(data):
        raise FdtError("the header states %d bytes and %d are there" % (total, len(data)))
    if off_struct + size_struct > total or off_strings + size_strings > total or off_rsv >= total:
        raise FdtError("a block lies outside the blob")

    reserve, pos = [], off_rsv
    while True:
        if pos + 16 > total:
            raise FdtError("the memory reservation block has no end")
        addr, size = struct.unpack_from(">QQ", data, pos)
        pos += 16
        if addr == 0 and size == 0:
            break
        reserve.append((addr, size))

    def name_at(off):
        end = data.find(b"\0", off_strings + off, off_strings + size_strings)
        if off >= size_strings or end < 0:
            raise FdtError("a property name lies outside the strings block")
        return data[off_strings + off:end].decode("utf-8", "backslashreplace")

    def aligned(p):
        return off_struct + ((p - off_struct + 3) & ~3)

    pos, end, root, cur, depth = off_struct, off_struct + size_struct, None, None, 0
    while True:
        if pos + 4 > end:
            raise FdtError("the structure block has no end")
        (token,) = struct.unpack_from(">I", data, pos)
        pos += 4
        if token == NOP:
            continue
        if token == END:
            break
        if token == BEGIN_NODE:
            z = data.find(b"\0", pos, end)
            if z < 0:
                raise FdtError("a node name has no end")
            node = Node(data[pos:z].decode("utf-8", "backslashreplace"), cur)
            pos = aligned(z + 1)
            if cur is not None and any(n.name == node.name for n in cur.children):
                raise FdtError("%s has two nodes named %s" % (cur.path, node.name))
            depth += 1
            if depth > MAX_DEPTH + 1:
                raise FdtError("nodes nest deeper than %d, which is as deep as the kernel reads" % MAX_DEPTH)
            if cur is not None:
                cur.children.append(node)
            elif root is None:
                root = node
            else:
                raise FdtError("more than one root node")
            cur = node
        elif token == END_NODE:
            if cur is None:
                raise FdtError("a node ends that never began")
            cur = cur.parent
            depth -= 1
        elif token == PROP:
            if cur is None or pos + 8 > end:
                raise FdtError("a property lies outside any node")
            length, name_off = struct.unpack_from(">II", data, pos)
            pos += 8
            if pos + length > end:
                raise FdtError("a property value runs past the structure block")
            name = name_at(name_off)
            if name in cur.props:
                raise FdtError("%s states %s twice" % (cur.path, name))
            cur.props[name] = bytes(data[pos:pos + length])
            pos = aligned(pos + length)
        else:
            raise FdtError("unknown token %#x" % token)
    if root is None or cur is not None:
        raise FdtError("the structure block is incomplete")
    return Tree(root, reserve, {"totalsize": total, "version": version, "boot_cpuid_phys": boot_cpu,
                                "struct": size_struct, "strings": size_strings})


def load(path):
    with open(path, "rb") as f:
        return parse(f.read())


def render(value, limit=None):
    if not value:
        return ""
    if value[-1] == 0:
        parts = value[:-1].split(b"\0")
        if all(p and all(32 <= b < 127 for b in p) for p in parts):
            return ", ".join('"%s"' % p.decode() for p in parts)
    if len(value) % 4 == 0:
        cells = struct.unpack(">%dI" % (len(value) // 4), value)
        shown = cells if limit is None else cells[:limit]
        tail = " ... %d cells>" % len(cells) if len(shown) < len(cells) else ">"
        return "<" + " ".join("%#x" % x for x in shown) + tail
    shown = value if limit is None else value[:limit * 4]
    tail = " ... %d bytes]" % len(value) if len(shown) < len(value) else "]"
    return "[" + " ".join("%02x" % b for b in shown) + tail


def _join(cells):
    v = 0
    for x in cells:
        v = (v << 32) | x
    return v


def ranges(node, prop="reg"):
    up = node.parent
    ac = up.cell("#address-cells", 2) if up else 2
    sc = up.cell("#size-cells", 1) if up else 1
    cells, step = node.cells(prop), ac + sc
    if step == 0:
        return []
    return [(_join(cells[i:i + ac]), _join(cells[i + ac:i + step]))
            for i in range(0, len(cells) - step + 1, step)]


def human(n):
    for unit, shift in (("GiB", 30), ("MiB", 20), ("KiB", 10)):
        if n >= 1 << shift:
            return ("%d %s" if n % (1 << shift) == 0 else "%.1f %s") % (n / (1 << shift), unit)
    return "%d bytes" % n


def span(base, size):
    return "%#x - %#x (%s)" % (base, base + size - 1, human(size)) if size else "%#x (empty)" % base


def summary(tree):
    root = tree.root
    nodes = list(root.walk())
    depth = 0
    for n in nodes:
        d, up = 0, n.parent
        while up is not None:
            d, up = d + 1, up.parent
        depth = max(depth, d)

    memory = []
    for n in nodes:
        if n.text("device_type") != "memory":
            continue
        memory.append({"path": n.path, "reg": ranges(n),
                       "usable": ranges(n, "linux,usable-memory") if "linux,usable-memory" in n.props else None,
                       "numa": n.cell("numa-node-id"), "status": n.text("status")})

    reserved = []
    rm = root.child("reserved-memory")
    for n in rm.children if rm is not None and rm.name == "reserved-memory" else []:
        sc = rm.cell("#size-cells", 1)
        reserved.append({"path": n.path, "reg": ranges(n),
                         "size": _join(n.cells("size")[:sc]) if "size" in n.props else None,
                         "flags": [k for k in ("no-map", "reusable") if k in n.props],
                         "compatible": (n.strings("compatible") or [None])[0]})

    cpus = [n for n in nodes if n.text("device_type") == "cpu"]
    models = {}
    for n in cpus:
        k = (n.strings("compatible") or ["?"])[0]
        models[k] = models.get(k, 0) + 1

    chosen = root.child("chosen")
    chosen = chosen if chosen is not None and chosen.name == "chosen" else None
    stdout = chosen.text("stdout-path") if chosen is not None else None
    stdout_node = None
    if stdout:
        target = stdout.split(":", 1)[0]
        aliases = root.child("aliases")
        if not target.startswith("/") and aliases is not None:
            target = aliases.text(target) or target
        stdout_node = target if tree.node(target) is not None and target.startswith("/") else None

    groups = {}
    for n in nodes:
        compat = n.strings("compatible")
        if n.parent is None or not compat:
            continue
        key = (n.parent.path, compat[0], n.text("status"))
        g = groups.setdefault(key, {"path": n.path, "compatible": compat[0], "status": n.text("status"),
                                    "count": 0})
        g["count"] += 1

    return {
        "model": root.text("model"),
        "compatible": root.strings("compatible"),
        "address_cells": root.cell("#address-cells", 2),
        "size_cells": root.cell("#size-cells", 1),
        "memory": memory,
        "memreserve": [list(r) for r in tree.reserve],
        "reserved": reserved,
        "cpus": {"count": len(cpus), "models": models,
                 "enable_method": sorted({m for n in cpus for m in n.strings("enable-method")}),
                 "disabled": sum(1 for n in cpus if not n.enabled())},
        "chosen": None if chosen is None else {
            "bootargs": chosen.text("bootargs"), "stdout": stdout, "stdout_node": stdout_node,
            "properties": {k: render(v, SHOWN_CELLS) for k, v in chosen.props.items()}},
        "interrupt_controllers": [{"path": n.path, "compatible": (n.strings("compatible") or [None])[0]}
                                  for n in nodes if "interrupt-controller" in n.props],
        "devices": list(groups.values()),
        "disabled": [n.path for n in nodes if not n.enabled()],
        "nodes": len(nodes),
        "properties": sum(len(n.props) for n in nodes),
        "depth": depth,
        "blob": tree.header,
    }


def show(s):
    rows = []

    def row(label, *values):
        for i, v in enumerate(values):
            rows.append((label if i == 0 else "", v))

    row("model", "%s%s" % (s["model"] or "-", ("  compatible " + ", ".join(s["compatible"])) if s["compatible"] else ""))
    row("cells", "#address-cells %d, #size-cells %d" % (s["address_cells"], s["size_cells"]))
    lines = []
    for m in s["memory"]:
        for base, size in m["reg"] or [(None, None)]:
            note = "".join([("  node %d" % m["numa"]) if m["numa"] is not None else "",
                            ("  status " + m["status"]) if m["status"] else ""])
            lines.append("%s  %s%s" % (m["path"], span(base, size) if base is not None else "no reg", note))
        for base, size in m["usable"] or []:
            lines.append("%s  linux,usable-memory %s" % (m["path"], span(base, size)))
    row("memory", *(lines or ["none"]))
    if s["memreserve"]:
        row("memreserve", *[span(a, n) for a, n in s["memreserve"]])
    lines = []
    for r in s["reserved"]:
        where = ", ".join(span(a, n) for a, n in r["reg"]) if r["reg"] else (
            "%s placed at boot" % human(r["size"]) if r["size"] is not None else "no reg")
        lines.append("  ".join(filter(None, [r["path"], where, " ".join(r["flags"]), r["compatible"]])))
    if lines:
        row("reserved", *lines)
    cp = s["cpus"]
    row("cpus", "%d%s%s%s" % (
        cp["count"],
        ("  " + ", ".join("%s x%d" % kv if kv[1] > 1 else kv[0] for kv in cp["models"].items())) if cp["models"] else "",
        ("  enable-method " + ", ".join(cp["enable_method"])) if cp["enable_method"] else "",
        ("  %d disabled" % cp["disabled"]) if cp["disabled"] else ""))
    ch = s["chosen"]
    if ch is not None:
        lines = []
        if ch["stdout"]:
            lines.append("stdout-path %s%s" % (ch["stdout"], ("  -> " + ch["stdout_node"])
                                               if ch["stdout_node"] and ch["stdout_node"] != ch["stdout"] else ""))
        if ch["bootargs"] is not None:
            lines.append('bootargs "%s"' % ch["bootargs"])
        rest = [k for k in ch["properties"] if k not in ("stdout-path", "bootargs")]
        if rest:
            lines.append("also " + ", ".join(rest))
        row("chosen", *(lines or ["empty"]))
    if s["interrupt_controllers"]:
        row("interrupts", *["%s  %s" % (i["path"], i["compatible"] or "-") for i in s["interrupt_controllers"]])
    row("devices", *["%s  %s%s%s" % (d["path"], d["compatible"], ("  x%d" % d["count"]) if d["count"] > 1 else "",
                                     ("  status " + d["status"]) if d["status"] else "")
                     for d in s["devices"]] or ["none"])
    b = s["blob"]
    row("size", "%d nodes, %d properties, depth %d; %d bytes (structure %d, strings %d), version %d" % (
        s["nodes"], s["properties"], s["depth"], b["totalsize"], b["struct"], b["strings"], b["version"]))
    width = max(len(k) for k, _ in rows) + 2
    return "\n".join("  %s%s" % (c("36", k.ljust(width)), v) for k, v in rows)


def diff(a, b):
    out = []

    def below(node):
        n = sum(1 for _ in node.walk()) - 1
        return "node%s" % ((", %d below" % n) if n else "")

    def visit(x, y):
        path = x.path
        for k in list(x.props) + [k for k in y.props if k not in x.props]:
            if k not in y.props:
                out.append("- %s %s" % (path, k))
            elif k not in x.props:
                out.append("+ %s %s%s" % (path, k, (": " + render(y.props[k], SHOWN_CELLS)) if y.props[k] else ""))
            elif x.props[k] != y.props[k] and (path, k) not in SEEDS:
                out.append("~ %s %s: %s -> %s" % (path, k, render(x.props[k], SHOWN_CELLS) or "(empty)",
                                                  render(y.props[k], SHOWN_CELLS) or "(empty)"))
        theirs = {n.name: n for n in y.children}
        for n in x.children:
            if n.name in theirs:
                visit(n, theirs.pop(n.name))
            else:
                out.append("- %s (%s)" % (n.path, below(n)))
        for n in theirs.values():
            out.append("+ %s (%s)" % (n.path, below(n)))

    visit(a.root, b.root)
    if a.reserve != b.reserve:
        out.append("~ memreserve: %s -> %s" % (", ".join(span(*r) for r in a.reserve) or "none",
                                              ", ".join(span(*r) for r in b.reserve) or "none"))
    return out


def carried(want, got):
    out = []

    def visit(x, y, chosen):
        path = x.path
        for k, v in x.props.items():
            if (path, k) in SEEDS:
                continue
            if k not in y.props:
                out.append("- %s %s" % (path, k))
            elif y.props[k] != v:
                out.append("~ %s %s: %s -> %s" % (path, k, render(v, SHOWN_CELLS) or "(empty)",
                                                  render(y.props[k], SHOWN_CELLS) or "(empty)"))
        if not chosen:
            for k in y.props:
                if k not in x.props:
                    out.append("+ %s %s%s" % (path, k, (": " + render(y.props[k], SHOWN_CELLS)) if y.props[k] else ""))
        theirs = {n.name: n for n in y.children}
        for n in x.children:
            if n.name in theirs:
                visit(n, theirs.pop(n.name), chosen or n.path == "/chosen")
            else:
                out.append("- %s (node)" % n.path)
        if not chosen:
            out.extend("+ %s (node)" % n.path for n in theirs.values() if n.path != "/chosen")

    visit(want.root, got.root, False)
    out.extend("- memreserve %s" % span(*r) for r in want.reserve if r not in got.reserve)
    return out


def identity(tree):
    h = hashlib.sha256()

    def feed(node):
        if node.path == "/chosen":
            return
        h.update(b"N" + node.name.encode() + b"\0")
        for k in sorted(node.props):
            h.update(b"P" + k.encode() + b"\0" + struct.pack(">I", len(node.props[k])) + node.props[k])
        for n in sorted(node.children, key=lambda n: n.name):
            feed(n)
        h.update(b"E")

    feed(tree.root)
    for addr, size in tree.reserve:
        h.update(struct.pack(">QQ", addr, size))
    return h.hexdigest()


def is_overlay(tree):
    return any(any(n.name == "__overlay__" for n in top.children) for top in tree.root.children)


def image_size(path):
    with open(path, "rb") as f:
        head = f.read(64)
    if len(head) < 64 or head[56:60] not in IMAGE_MAGICS:
        return None
    return struct.unpack_from("<Q", head, 16)[0] or None


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0
    cmd, args = argv[0], argv[1:]
    try:
        if cmd == "summary" and args:
            s = summary(load(args[0]))
            print(json.dumps(s, indent=1) if "--json" in args else show(s))
            return 0
        if cmd == "diff" and len(args) == 2:
            lines = diff(load(args[0]), load(args[1]))
            if lines:
                print("\n".join(lines))
            return 1 if lines else 0
        if cmd == "id" and args:
            print(identity(load(args[0])))
            return 0
        if cmd == "check" and args:
            load(args[0])
            return 0
        if cmd == "kind" and args:
            print("overlay" if is_overlay(load(args[0])) else "tree")
            return 0
        if cmd == "carried" and len(args) == 2:
            lines = carried(load(args[0]), load(args[1]))
            if lines:
                print("\n".join(lines))
            return 1 if lines else 0
        if cmd == "prop" and len(args) == 3:
            node = load(args[0]).node(args[1])
            if node is None or args[2] not in node.props:
                return 1
            print(render(node.props[args[2]]))
            return 0
        if cmd == "image-size" and args:
            n = image_size(args[0])
            if n is None:
                return 1
            print(n)
            return 0
    except (OSError, FdtError) as e:
        print("fdt.py: %s: %s" % (args[0] if args else cmd, e), file=sys.stderr)
        return 2
    print("fdt.py: unknown action %r" % cmd, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
