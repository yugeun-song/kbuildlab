#!/usr/bin/env python3
"""Attach a device tree to a tree, boot it, and hold what the kernel got against
what was attached; then put the tree back as it was.

    dt-guest.py TREE [RUN-OPTION...]

The attached tree is the tree's base with its first memory range halved, made as
an overlay from what `kbuildlab dt --json` reports, so nothing here knows a
machine.  Refuses a tree that already has a device tree of its own.  The guest is
driven as tests/guest.py drives it, and the same environment variables apply.
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
    memory = next((m for m in s["memory"] if m["reg"] and m["status"] in (None, "okay")), None)
    if memory is None:
        print("dt-guest.py: the base tree of %s has no memory node to change" % tree, file=sys.stderr)
        return 2
    ranges = [list(r) for r in memory["reg"]]
    whole = sum(size for _, size in ranges)
    ranges[0][1] //= 2
    half = sum(size for _, size in ranges)
    reg = " ".join("%s %s" % (cells(b, s["address_cells"]), cells(n, s["size_cells"])) for b, n in ranges)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            overlay = os.path.join(tmp, "half-memory.dtso")
            with open(overlay, "w") as f:
                f.write("/dts-v1/;\n/plugin/;\n&{%s} { reg = <%s>; };\n" % (memory["path"], reg))
            rc, out = kbl("dt", "apply", tree, overlay)
            if not check(
                rc == 0 and "attached" in out, "an overlay that halves %s is built and attached" % memory["path"], out
            ):
                return 1
        rc, out = kbl("dt", "check", tree)
        check(rc == 0, "dt check holds before the boot", out)
        rc, out = kbl("dt", "diff", tree)
        check(
            rc == 0 and out.count("\n") == 1 and memory["path"] + " reg" in out,
            "it differs from the base in that reg alone",
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
        rc, out = kbl("run", tree, "--run", "--smp", smp, *options, timeout=120)
        check(
            rc != 0 and "is not the one" in out,
            "a machine with %s CPUs, which the tree was not built for, is refused" % smp,
            out,
        )

        g = guest.Guest(tree, options, float(os.environ.get("KBL_GUEST_TIMEOUT", "900")))
        try:
            g.login()
            _, text = g.run(0, "base64 /sys/firmware/fdt | tr -d '\\n'; echo")
            _, total = g.run(1, "grep MemTotal /proc/meminfo")
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
        node = got.node(memory["path"])
        check(
            node is not None and fdt.ranges(node) == [tuple(r) for r in ranges],
            "its %s is the halved one" % memory["path"],
        )
        kb = int(re.search(r"MemTotal:\s+(\d+)", total).group(1))
        check(
            half * 0.8 < kb * 1024 <= half,
            "MemTotal is %d kB: within the %d MiB the tree states, not the %d MiB of the machine"
            % (kb, half >> 20, whole >> 20),
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
