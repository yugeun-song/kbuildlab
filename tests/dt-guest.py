#!/usr/bin/env python3
"""Hold a tree's own device tree to the machine, end to end: one that states
other hardware is refused, one that only sets memory aside is attached and
booted, and what the kernel then reports is held against it.  The tree is put
back as it was.

    dt-guest.py TREE [RUN-OPTION...]

The overlays are made from what `kbuildlab dt --json` reports, so nothing here
knows a machine: one halves the first memory range and one sets aside a page
past the end of memory (both refused), one sets aside a region in the middle of
the first range (attached).  Refuses a tree that already has a device tree of
its own.  The guest is driven as tests/guest.py drives it, and the same
environment variables apply.
"""

import base64
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile

here = os.path.dirname(os.path.abspath(__file__))
repo = os.path.dirname(here)
sys.path.insert(0, os.path.join(repo, "lib"))
import fdt  # noqa: E402

spec = importlib.util.spec_from_file_location("guest", os.path.join(here, "guest.py"))
guest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guest)

KBL = os.path.join(repo, "bin", "kbuildlab")
RESERVED = "/reserved-memory"
HOLE = 64 << 20
failed = 0
checked = 0


def check(ok, what, detail=""):
    global failed, checked
    checked += 1
    print(
        "%s %s%s"
        % (
            "ok  " if ok else "FAIL",
            what,
            ("\n     " + str(detail).replace("\n", "\n     ")) if detail and not ok else "",
        )
    )
    if not ok:
        failed += 1
    return ok


def kbl(*args, timeout=600, stdout_only=False):
    env = dict(os.environ, NO_COLOR="1", VISUAL="", EDITOR="")
    r = subprocess.run(
        os.environ.get("KBL_GUEST_PREFIX", "").split() + [KBL, *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
    )
    return r.returncode, r.stdout if stdout_only and r.returncode == 0 else r.stdout + r.stderr


def cells(value, count):
    return " ".join("0x%x" % ((value >> (32 * (count - 1 - i))) & 0xFFFFFFFF) for i in range(count))


def joined(spans):
    out = []
    for base, size in sorted(s for s in spans if s[1]):
        if out and base <= out[-1][0] + out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][0] + out[-1][1], base + size) - out[-1][0])
        else:
            out.append((base, size))
    return out


def without(spans, holes):
    out = []
    for base, size in spans:
        parts = [(base, base + size)]
        for at, length in holes:
            rest = []
            for lo, hi in parts:
                if at + length <= lo or hi <= at:
                    rest.append((lo, hi))
                    continue
                if lo < at:
                    rest.append((lo, at))
                if at + length < hi:
                    rest.append((at + length, hi))
            parts = rest
        out.extend((lo, hi - lo) for lo, hi in parts)
    return joined(out)


def spans(ranges):
    return ", ".join("%#x-%#x" % (b, b + n - 1) for b, n in ranges) or "none"


def unmapped(tree):
    node = next((n for n in tree.root.children if n.path == RESERVED), None)
    return [r for n in (node.children if node else []) if n.enabled() and "no-map" in n.props for r in fdt.ranges(n)]


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0
    tree, options = argv[0], argv[1:]
    rc, out = kbl("dt", tree, "--json", stdout_only=True)
    if rc != 0:
        print(out, file=sys.stderr)
        return 2
    state = json.loads(out)
    if state.get("device_tree", "") is None:
        print("dt-guest.py: %s boots without a device tree" % tree)
        return 2
    d = state["directory"]
    if state["in_force"] == "custom" or os.path.exists(os.path.join(d, "custom.dts")):
        print(
            "dt-guest.py: %s has a device tree of its own in %s; this test would replace it" % (tree, d),
            file=sys.stderr,
        )
        return 2
    s = state["structure"]
    stated = [m for m in s["memory"] if m["reg"] and m["status"] in (None, "okay")]
    if not stated or stated[0]["reg"][0][1] < 4 * HOLE:
        print("dt-guest.py: the base tree of %s has too little memory to set some aside" % tree, file=sys.stderr)
        return 2
    memory = stated[0]
    ac, sc = s["address_cells"], s["size_cells"]
    start, size = memory["reg"][0]
    end = max(b + n for m in stated for b, n in m["reg"])
    hole = ((start + size // 2) & ~((2 << 20) - 1), HOLE)
    has = any(n.path == RESERVED for n in fdt.load(state["shown"]).root.children)

    def halved(tmp):
        reg = [list(r) for r in memory["reg"]]
        reg[0][1] //= 2
        text = " ".join("%s %s" % (cells(b, ac), cells(n, sc)) for b, n in reg)
        return write(tmp, "half-memory.dtso", "&{%s} { reg = <%s>; };" % (memory["path"], text))

    def aside(tmp, name, at, length):
        child = "%s@%x { reg = <%s %s>; no-map; };" % (name, at, cells(at, ac), cells(length, sc))
        if has:
            return write(tmp, name + ".dtso", "&{%s} { %s };" % (RESERVED, child))
        head = "#address-cells = <%d>; #size-cells = <%d>; ranges;" % (ac, sc)
        return write(tmp, name + ".dtso", "&{/} { reserved-memory { %s %s }; };" % (head, child))

    def write(tmp, name, body):
        path = os.path.join(tmp, name)
        with open(path, "w") as f:
            f.write("/dts-v1/;\n/plugin/;\n%s\n" % body)
        return path

    def own():
        rc, out = kbl("dt", tree, "--json", stdout_only=True)
        return rc == 0 and (json.loads(out)["in_force"] == "custom" or os.path.exists(os.path.join(d, "custom.dts")))

    try:
        with tempfile.TemporaryDirectory() as tmp:
            rc, out = kbl("dt", "apply", tree, halved(tmp))
            check(
                rc != 0 and "does not agree with the machine" in out and memory["path"] + " reg" in out and not own(),
                "an overlay that halves %s is refused, and nothing is attached" % memory["path"],
                out,
            )
            rc, out = kbl("dt", "apply", tree, aside(tmp, "kbl-past", end, 4096))
            check(
                rc != 0 and "lies outside the machine's memory" in out and not own(),
                "a page set aside at %#x, past the end of memory, is refused" % end,
                out,
            )
            rc, out = kbl("dt", "apply", tree, aside(tmp, "kbl-hole", *hole))
            if not check(
                rc == 0 and "attached" in out,
                "%d MiB set aside at %#x, inside the memory, is built and attached" % (hole[1] >> 20, hole[0]),
                out,
            ):
                return 1
        rc, out = kbl("dt", "check", tree)
        check(rc == 0 and "agrees" in out, "dt check holds before the boot", out)
        rc, out = kbl("dt", "diff", tree)
        check(
            rc == 0 and out.count("\n") == 1 and RESERVED in out,
            "it differs from the base in %s alone" % RESERVED,
            out,
        )
        custom = fdt.load(os.path.join(d, "custom.dtb"))

        source = os.path.join(d, "custom.dts")
        with open(source) as f:
            kept = f.read()
        with open(source, "a") as f:
            f.write("\n")
        rc, out = kbl("run", tree, "--run", *options, timeout=120)
        check(
            rc != 0 and "changed since" in out,
            "a source edited after its build is refused before the guest starts",
            out,
        )
        with open(source, "w") as f:
            f.write(kept)
        smp = str(s["cpus"]["count"] + 1)
        rc, out = kbl("run", tree, "--run", "--smp", smp, *options, timeout=300)
        check(
            rc != 0 and "is not the one" in out,
            "a run with %s CPUs, which is not the machine tree.conf states, is refused" % smp,
            out,
        )

        g = guest.Guest(tree, options, float(os.environ.get("KBL_GUEST_TIMEOUT", "900")))
        try:
            g.login()
            _, text = g.run(0, "base64 /sys/firmware/fdt | tr -d '\\n'; echo")
            _, iomem = g.run(1, "grep -i 'system ram' /proc/iomem")
            _, total = g.run(2, "grep MemTotal /proc/meminfo")
            status = g.off()
        except RuntimeError as e:
            check(False, "the guest boots on the attached tree", "%s\n%s" % (e, "\n".join(g.text.splitlines()[-30:])))
            return 1
        finally:
            g.kill()
            log = os.environ.get("KBL_GUEST_LOG")
            if log:
                with open(log, "w") as f:
                    f.write(g.text)
        check(status == 0, "kbuildlab run ends with status 0", status)
        got = fdt.parse(base64.b64decode(re.sub(r"[^A-Za-z0-9+/=]", "", text)))
        lost = fdt.carried(custom, got)
        check(lost == [], "the kernel's /sys/firmware/fdt carries the attached tree whole", "\n".join(lost))
        check(hole in unmapped(got), "it sets %#x aside, unmapped" % hole[0], spans(unmapped(got)))
        check(
            fdt.memory(got) == joined(r for m in stated for r in map(tuple, m["reg"])),
            "its memory is the machine's",
            spans(fdt.memory(got)),
        )
        ram = joined(
            (int(lo, 16), int(hi, 16) + 1 - int(lo, 16))
            for lo, hi in re.findall(r"^([0-9a-f]+)-([0-9a-f]+) : System RAM\s*$", iomem, re.M | re.I)
        )
        want = without(fdt.memory(got), unmapped(got))
        check(
            ram == want,
            "the kernel's System RAM is that memory without what the tree leaves unmapped",
            "/proc/iomem  %s\nthe tree     %s" % (spans(ram), spans(want)),
        )
        usable = sum(n for _, n in want)
        kb = int(re.search(r"MemTotal:\s+(\d+)", total).group(1))
        check(
            usable * 0.8 < kb * 1024 <= usable,
            "MemTotal is %d kB, within the %d MiB that leaves" % (kb, usable >> 20),
        )
        booted = os.path.join(d, "booted.dtb")
        if check(os.path.isfile(booted), "the launcher kept what the kernel was handed (booted.dtb)"):
            check(
                fdt.diff(fdt.load(booted), got) == [],
                "booted.dtb is what the guest's own /sys/firmware/fdt holds",
                "\n".join(fdt.diff(fdt.load(booted), got)),
            )
        check("was handed" in g.text and "as it is" in g.text, "the launcher says the tree was handed on as it is")
        rc, out = kbl("dt", "check", tree)
        check(rc == 0 and "handed the kernel this tree as it is" in out, "dt check holds after the boot", out)
        rc, out = kbl("dt", "detach", tree)
        check(rc == 0, "detach", out)
        rc, out = kbl("dt", tree, "--json", stdout_only=True)
        check(rc == 0 and json.loads(out)["in_force"] != "custom", "detached, the base is in force again", out)
    finally:
        rc, out = kbl("dt", "reset", tree, "--yes")
        check(
            rc == 0
            and not os.path.exists(os.path.join(d, "custom.dts"))
            and not os.path.exists(os.path.join(d, "attached")),
            "reset leaves the tree as it was found",
            out,
        )
    print("%d checks, %s" % (checked, "all hold" if not failed else "%d failed" % failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
