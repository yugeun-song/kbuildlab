#!/usr/bin/env python3
"""Hold a tree's own device tree to the machine, end to end: the machine takes
its memory and its CPUs from the tree, a tree that states hardware qemu has no
machine for is refused, and what the booted kernel reports is held against the
tree, from inside the guest and by `kbuildlab dt check` from outside.  The tree
is put back as it was.

    dt-guest.py TREE [RUN-OPTION...]

The trees are made from what `kbuildlab dt --json` reports, so nothing here knows
a machine: one moves the first memory range and one sets aside a page past the
end of memory (both refused); one halves the first memory range and one sets
aside a region in the middle of what is left (both attached, one on the other);
the last leaves out a CPU, which is refused as it stands and attached once it is
rebased.  Refuses a tree that already has a device tree of its own.  The guest is
driven as tests/guest.py drives it, and the same environment variables apply.
"""

import base64
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

here = os.path.dirname(os.path.abspath(__file__))
repo = os.path.dirname(here)
sys.path.insert(0, os.path.join(repo, "lib"))
import fdt  # noqa: E402

spec = importlib.util.spec_from_file_location("guest", os.path.join(here, "guest.py"))
if spec is None or spec.loader is None:
    raise SystemExit("dt-guest.py: cannot load tests/guest.py")
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
    d, source = state["directory"], state["source"]
    if state["in_force"] == "custom" or os.path.exists(source):
        print(
            "dt-guest.py: %s has a device tree of its own, %s; this test would replace it" % (tree, source),
            file=sys.stderr,
        )
        return 2
    s = state["structure"]
    stated = [m for m in s["memory"] if m["reg"] and m["status"] in (None, "okay")]
    if not stated or stated[0]["reg"][0][1] < 8 * HOLE:
        print(
            "dt-guest.py: the base tree of %s has too little memory to halve and set some aside" % tree, file=sys.stderr
        )
        return 2
    memory = stated[0]
    ac, sc = s["address_cells"], s["size_cells"]
    start, size = memory["reg"][0]
    whole = joined(r for m in stated for r in map(tuple, m["reg"]))
    end = max(b + n for b, n in whole)
    halved = [list(r) for r in memory["reg"]]
    halved[0][1] //= 2
    left = joined([tuple(r) for r in halved] + [r for m in stated[1:] for r in map(tuple, m["reg"])])
    hole = ((start + halved[0][1] // 2) & ~((2 << 20) - 1), HOLE)
    has = any(n.path == RESERVED for n in fdt.load(state["shown"]).root.children)

    def write(tmp, name, body):
        path = os.path.join(tmp, name)
        with open(path, "w") as f:
            f.write("/dts-v1/;\n/plugin/;\n%s\n" % body)
        return path

    def ranged(tmp, name, reg):
        text = " ".join("%s %s" % (cells(b, ac), cells(n, sc)) for b, n in reg)
        return write(tmp, name, "&{%s} { reg = <%s>; };" % (memory["path"], text))

    def aside(tmp, name, at, length):
        child = "%s@%x { reg = <%s %s>; no-map; };" % (name, at, cells(at, ac), cells(length, sc))
        if has:
            return write(tmp, name + ".dtso", "&{%s} { %s };" % (RESERVED, child))
        head = "#address-cells = <%d>; #size-cells = <%d>; ranges;" % (ac, sc)
        return write(tmp, name + ".dtso", "&{/} { reserved-memory { %s %s }; };" % (head, child))

    def own():
        rc, out = kbl("dt", tree, "--json", stdout_only=True)
        return rc == 0 and (json.loads(out)["in_force"] == "custom" or os.path.exists(source))

    try:
        with tempfile.TemporaryDirectory() as tmp:
            moved = [[b + (16 << 20), n] for b, n in memory["reg"]]
            rc, out = kbl("dt", "apply", tree, ranged(tmp, "moved.dtso", moved))
            check(
                rc != 0 and "does not agree with the machine" in out and memory["path"] + " reg" in out and not own(),
                "memory moved to %#x, where qemu has no machine with it, is refused" % moved[0][0],
                out,
            )
            rc, out = kbl("dt", "apply", tree, aside(tmp, "kbl-past", end, 4096))
            check(
                rc != 0 and "lies outside the machine's memory" in out and not own(),
                "a page set aside at %#x, past the end of memory, is refused" % end,
                out,
            )
            rc, out = kbl("dt", "apply", tree, ranged(tmp, "half.dtso", halved))
            if not check(
                rc == 0 and "attached" in out and "%dM" % (sum(n for _, n in left) >> 20) in out,
                "%s halved is attached, and qemu is to be started with %d MiB"
                % (memory["path"], sum(n for _, n in left) >> 20),
                out,
            ):
                return 1
            check(
                fdt.memory(fdt.load(os.path.join(d, "machine.dtb"))) == left,
                "qemu, asked with that memory, states it as the tree does",
                spans(fdt.memory(fdt.load(os.path.join(d, "machine.dtb")))),
            )
            rc, out = kbl("dt", "apply", tree, aside(tmp, "kbl-hole", *hole))
            if not check(
                rc == 0 and "attached" in out,
                "%d MiB set aside at %#x, inside what is left, is attached on top" % (hole[1] >> 20, hole[0]),
                out,
            ):
                return 1
            cpus = s["cpus"]["count"]
            if cpus > 1:
                fewer = fdt.load(os.path.join(d, "custom.dtb"))
                last = [n for n in fewer.root.walk() if n.text("device_type") == "cpu"][-1]
                last.parent.children.remove(last)
                blob = os.path.join(tmp, "fewer.dtb")
                with open(blob, "wb") as f:
                    f.write(fdt.write(fewer))
                rc, out = kbl("dt", "apply", tree, blob)
                check(
                    rc != 0 and "does not agree with the machine" in out and "dt rebase" in out,
                    "the same tree without %s is refused as it stands, and rebase is named" % last.path,
                    out,
                )
                rc, out = kbl("dt", "rebase", tree, blob)
                if not check(
                    rc == 0 and "attached" in out and "smp %d" % (cpus - 1) in out,
                    "rebased, it is attached, and qemu is to be started with %d CPU(s)" % (cpus - 1),
                    out,
                ):
                    return 1
                cpus -= 1
        rc, out = kbl("dt", "check", tree)
        check(rc == 0 and "agrees" in out, "dt check holds before the boot", out)
        rc, out = kbl("dt", "diff", tree)
        check(
            rc == 0 and out.count("\n") == 1 and RESERVED in out,
            "it differs from what qemu and the firmware hand on in %s alone" % RESERVED,
            out,
        )
        custom = fdt.load(os.path.join(d, "custom.dtb"))

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
            rc != 0 and ("is not the one" in out or "is not the number of CPUs" in out),
            "a run with %s CPUs, which is not the machine the tree states, is refused" % smp,
            out,
        )
        rc, out = kbl("run", tree, "--run", "--mem", "%dM" % (sum(n for _, n in whole) >> 20), *options, timeout=120)
        check(
            rc != 0 and "is not the memory" in out,
            "a run with other memory than the tree states is refused",
            out,
        )

        g = guest.Guest(tree, options, float(os.environ.get("KBL_GUEST_TIMEOUT", "900")))
        try:
            g.login()
            _, text = g.run(0, "base64 /sys/firmware/fdt | tr -d '\\n'; echo")
            _, iomem = g.run(1, "grep -i 'system ram' /proc/iomem")
            _, total = g.run(2, "grep MemTotal /proc/meminfo")
            _, possible = g.run(3, "cat /sys/devices/system/cpu/possible")
            _, online = g.run(4, "nproc")
            live = kbl("dt", "check", tree)
            with tempfile.TemporaryDirectory() as tmp:
                shutil.copy(os.path.join(d, "custom.dtb"), os.path.join(tmp, "kept.dtb"))
                other = kbl("dt", "apply", tree, aside(tmp, "kbl-other", hole[0] + HOLE, HOLE))
                stale = kbl("dt", "check", tree)
                back = kbl("dt", "apply", tree, os.path.join(tmp, "kept.dtb"))
            again = kbl("dt", "check", tree)
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
        check(
            "the size the tree's own device tree states" in g.text,
            "the launcher says it took the memory from the tree",
        )
        got = fdt.parse(base64.b64decode(re.sub(r"[^A-Za-z0-9+/=]", "", text)))
        lost = fdt.carried(custom, got)
        check(lost == [], "the kernel's /sys/firmware/fdt carries the attached tree whole", "\n".join(lost))
        check(hole in unmapped(got), "it sets %#x aside, unmapped" % hole[0], spans(unmapped(got)))
        check(fdt.memory(got) == left, "its memory is the halved one", spans(fdt.memory(got)))
        stated = sum(1 for n in got.root.walk() if n.text("device_type") == "cpu")
        known = sum(
            int(hi or lo) - int(lo) + 1 for lo, hi in re.findall(r"(\d+)(?:-(\d+))?", possible.strip().splitlines()[-1])
        )
        check(
            stated == cpus and known == cpus and online.strip().splitlines()[-1] == str(cpus),
            "the tree states %d CPU(s), and the kernel knows and runs as many" % cpus,
            "tree %d, possible %r, nproc %r" % (stated, possible, online),
        )
        if cpus != s["cpus"]["count"]:
            check(
                "the number the tree's own device tree states" in g.text,
                "the launcher says it took the CPUs from the tree",
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
        found = re.search(r"MemTotal:\s+(\d+)", total)
        kb = int(found.group(1)) if found else 0
        check(
            usable * 0.8 < kb * 1024 <= usable,
            "MemTotal is %d kB, within the %d MiB that leaves" % (kb, usable >> 20),
            total,
        )
        rows = re.findall(r"^\s*(\S+) (running|live \w+) ", live[1], re.M)
        gone = fdt.human(sum(n for _, n in fdt.aside(custom)[0]))
        check(
            live[0] == 0
            and [name for _, name in rows] == ["running", "live tree", "live cpus", "live memory", "live aside"]
            and len({mark for mark, _ in rows}) == 1,
            "with the guest up, dt check reads its kernel from outside, and every row holds",
            live[1],
        )
        check(
            "the tree states %d, qemu runs %d, the kernel knows %d and has %d online" % (cpus, cpus, cpus, cpus)
            in live[1],
            "it finds the tree, qemu and the kernel at %d CPU(s)" % cpus,
            live[1],
        )
        size = fdt.human(sum(n for _, n in left))
        check(
            "the tree states %s, qemu has %s, and the kernel's System RAM is that without the %s the tree leaves unmapped"
            % (size, size, gone)
            in live[1],
            "and at %s of memory, with %s of it unmapped" % (size, gone),
            live[1],
        )
        check(
            other[0] == 0
            and stale[0] != 0
            and "the kernel holds another tree" in stale[1]
            and "is mapped, and the tree leaves it unmapped" in stale[1]
            and "the tree leaves it  " in stale[1],
            "once the tree sets aside more than the running kernel was handed, dt check tells in three rows",
            other[1] + stale[1],
        )
        check(
            back[0] == 0 and again[0] == 0, "and holds again with the tree the guest was started on", back[1] + again[1]
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
        check(
            rc == 0
            and json.loads(out)["in_force"] != "custom"
            and fdt.memory(fdt.load(json.loads(out)["shown"])) == whole,
            "detached, the base is in force again, with the memory tree.conf states",
            out,
        )
    finally:
        rc, out = kbl("dt", "reset", tree, "--yes")
        check(
            rc == 0 and not os.path.exists(source) and not os.path.exists(os.path.join(d, "attached")),
            "reset leaves the tree as it was found",
            out,
        )
    print("%d checks, %s" % (checked, "all hold" if not failed else "%d failed" % failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
