#!/usr/bin/env python3
"""Hold `kbuildlab dt` and `kbuildlab firmware` to their refusals: every wrong
call ends with kbuildlab's own message and a failing status, never with a shell
error, and leaves nothing half-made.  Runs against a registry of its own in a
temporary directory; nothing is built, booted or fetched."""

import os
import re
import subprocess
import sys
import tempfile

here = os.path.dirname(os.path.abspath(__file__))
repo = os.path.dirname(here)
KBL = os.path.join(repo, "bin", "kbuildlab")
SHELL_ERROR = re.compile(
    r"unbound variable|syntax error|command not found|bad substitution|"
    r"No such file or directory|ambiguous redirect|integer expression|Traceback"
)
ARM, RISCV, X86, OLD = "a" * 16, "b" * 16, "c" * 16, "d" * 16

failed = 0
checked = 0


def run(env, *args, stdin=None):
    r = subprocess.run([KBL, *args], env=env, capture_output=True, text=True, input=stdin, timeout=120)
    return r.returncode, re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", r.stdout + r.stderr)


def expect(env, args, status, *words, stdin=None):
    global failed, checked
    checked += 1
    rc, out = run(env, *args, stdin=stdin)
    why = []
    if (rc == 0) != (status == 0):
        why.append("status %d" % rc)
    why += ["no %r" % w for w in words if w not in out]
    m = SHELL_ERROR.search(out)
    if m:
        why.append("a shell error (%s)" % m.group(0))
    if why:
        failed += 1
        print(
            "FAIL kbuildlab %s: %s\n     %s"
            % (" ".join(args), ", ".join(why), out.strip().replace("\n", "\n     ")[:600])
        )


def tree(root, tid, arch, extra=""):
    d = os.path.join(root, "config", "kbuildlab", "trees", tid)
    os.makedirs(d)
    os.makedirs(os.path.join(root, "src", tid))
    with open(os.path.join(d, "tree.conf"), "w") as f:
        f.write("ARCH=%s\nSRC=%s\nVERSION=v0\n%s" % (arch, os.path.join(root, "src", tid), extra))
    return d


def main():
    global failed, checked
    with tempfile.TemporaryDirectory() as root:
        env = dict(
            os.environ,
            XDG_CONFIG_HOME=os.path.join(root, "config"),
            XDG_DATA_HOME=os.path.join(root, "data"),
            KBL_STATE_DIR=root,
            KBL_LAB=os.path.join(root, "lab"),
            NO_COLOR="1",
            EDITOR="",
            VISUAL="",
        )
        env.pop("KBL_WORKSPACE", None)
        arm = tree(root, ARM, "arm64", "BOOT=uboot\nMACHINE=virt\nCONSOLE=ttyAMA0\nGDB_PORT=1\nQMP_PORT=2\n")
        tree(root, RISCV, "riscv64", "BOOT=direct\n")
        tree(root, X86, "x86_64", "BOOT=uefi\n")
        old = tree(root, OLD, "arm64", "BOOT=uboot\nUBOOT=/nowhere/u-boot.bin\n")
        blob = os.path.join(root, "t.dtb")
        subprocess.run(
            ["dtc", "-q", "-I", "dts", "-O", "dtb", "-o", blob, "-"], input=b"/dts-v1/;\n/ { a = <1>; };\n", check=True
        )
        memory = b'memory@40000000 { device_type = "memory"; reg = <0 0x40000000 0 0x40000000>; };'
        cpu = b'cpus { #address-cells = <1>; #size-cells = <0>; cpu@0 { device_type = "cpu"; reg = <0>; }; };'
        sized, cpuless = os.path.join(root, "sized.dtb"), os.path.join(root, "cpuless.dtb")
        for path, nodes in ((sized, memory + cpu), (cpuless, memory)):
            subprocess.run(
                ["dtc", "-q", "-I", "dts", "-O", "dtb", "-o", path, "-"],
                input=b"/dts-v1/;\n/ { #address-cells = <2>; #size-cells = <2>; " + nodes + b" };\n",
                check=True,
            )
        junk = os.path.join(root, "junk.bin")
        with open(junk, "wb") as f:
            f.write(b"\xd0\x0d\xfe\xed" + os.urandom(64))
        empty = os.path.join(root, "empty.dts")
        open(empty, "w").close()

        expect(env, ["dt", "--bogus"], 1, "unknown option")
        expect(env, ["dt", ARM, "--stage"], 1, "--stage needs a name")
        expect(env, ["dt", ARM, "--stage", "nope"], 1, "no stage 'nope'")
        expect(env, ["dt", "show", ARM, "extra"], 1, "neither a registered tree nor expected here")
        expect(env, ["dt", "apply", ARM, "a", "b"], 1, "one file at a time")
        expect(env, ["dt", "diff", ARM, "a", "b", "c"], 1, "two stages or blobs at most")
        expect(env, ["dt", "detach", ARM, "--json"], 1, "belong to dt show")
        expect(env, ["dt", "edit", ARM, "--source"], 1, "belong to dt show")
        expect(env, ["dt", "apply", ARM, "--yes"], 1, "--yes belongs to dt reset")
        expect(env, ["dt", ARM, "--boot", "bogus"], 1, "unknown boot mode")
        expect(env, ["dt", ARM, "--board", "no-such-board"], 1, "no board")
        expect(env, ["dt", ARM, "--board", "../evil"], 1, "no board")
        expect(env, ["dt", ARM, "--board", "sifive_u"], 1, "is for riscv64")
        expect(env, ["dt", ARM, "--board", "imx8mp-evk", "--boot", "uboot"], 1, "contradicts --board")
        expect(env, ["dt", "ffff"], 1, "'ffff' is neither a registered tree nor expected here")
        expect(env, ["dt", "apply", "ffff", "x.dts"], 1, "one file at a time")
        expect(env, ["dt", X86], 0, "none for", "uefi")
        expect(env, ["dt", X86, "--json"], 0, '"device_tree": null')
        checked += 1
        if not subprocess.run([KBL, "dt", X86, "--json"], env=env, capture_output=True, text=True).stdout.startswith(
            "{"
        ):
            failed += 1
            print("FAIL dt --json prints something before its JSON")
        expect(env, ["dt", "check", X86], 0, "none")
        expect(env, ["dt", "edit", X86], 1, "has no device tree in a uefi boot")
        expect(env, ["dt", "apply", X86, blob], 1, "has no device tree in a uefi boot")
        expect(env, ["dt", "probe", X86], 1, "has no device tree in a uefi boot")
        expect(env, ["dt", "detach", X86], 0, "nothing was attached")
        expect(env, ["dt", "reset", X86], 0, "has no device tree of its own")
        expect(env, ["dt", ARM], 1, "did not hand over its device tree")
        expect(env, ["dt", "apply", ARM, blob], 1, "it states no memory", "cannot be held against a machine")
        expect(env, ["dt", "apply", ARM, cpuless], 1, "it states no CPUs", "cannot be held against a machine")
        with open(os.path.join(arm, "tree.conf"), "a") as f:
            f.write("SMP=sockets=2,cores=2\n")
        expect(env, ["dt", "apply", ARM, sized], 1, "does not begin with the number of CPUs")
        with open(os.path.join(arm, "tree.conf")) as f:
            conf = f.read()
        with open(os.path.join(arm, "tree.conf"), "w") as f:
            f.write(conf.replace("SMP=sockets=2,cores=2\n", "SMP=cpus=1,maxcpus=2\n"))
        expect(env, ["dt", "apply", ARM, sized], 1, "did not hand over its device tree")
        expect(env, ["dt", "rebase", ARM, sized], 1, "did not hand over its device tree")
        expect(env, ["dt", "rebase", ARM], 1, "has no device tree source of its own")
        expect(env, ["dt", "rebase", ARM, "a", "b"], 1, "one file at a time")
        expect(env, ["dt", "diff", ARM], 1, "did not hand over its device tree")
        expect(env, ["dt", "check", ARM], 1, "did not hand over its device tree")
        expect(env, ["dt", OLD], 1, "states UBOOT, which is no longer read")
        left = [
            n for n in os.listdir(os.path.join(root, "data", "kbuildlab", "trees", ARM, "dt", "uboot")) if n != ".lock"
        ]
        expect(env, ["dt", "detach", ARM], 0, "nothing was attached")
        checked += 1
        if left:
            failed += 1
            print("FAIL a refused probe left %s behind" % left)

        expect(env, ["firmware", "--bogus"], 1, "unknown option")
        expect(env, ["firmware", "show", ARM, RISCV], 1, "kbuildlab firmware [TREE]")
        expect(env, ["firmware", "--all"], 1, "kbuildlab firmware [TREE]")
        expect(env, ["firmware", "own", ARM, RISCV], 1, "one tree at a time")
        expect(env, ["firmware", "global", "--all"], 1, "one tree at a time")
        expect(env, ["firmware", "check", ARM, "--fresh"], 1, "--fresh belongs to firmware build")
        expect(env, ["firmware", "check", "ffff"], 1, "neither the ID of a registered tree nor a path")
        expect(env, ["firmware", X86], 1, "no global firmware for x86_64")
        expect(env, ["firmware", OLD], 1, "states UBOOT, which is no longer read")
        expect(env, ["firmware", "check", OLD], 1, "states UBOOT, which is no longer read")
        expect(env, ["firmware", ARM], 0, "global firmware for arm64", "not cloned")
        expect(env, ["firmware"], 0, ARM[:12], "global")
        expect(env, ["firmware", "check", ARM], 1, "is not cloned", "is not built")
        expect(env, ["firmware", "global", ARM], 0, "already boots the global firmware")
        expect(env, ["firmware", "own", ARM], 1, "has no", "firmware.conf")
        conf = os.path.join(arm, "firmware.conf")
        for body, word in (
            ("SRC=/x\nBUILD=/y\n", "states no IMAGE"),
            ("BUILD=/y\nIMAGE=u-boot.bin\n", "states no SRC"),
            ("SRC=/x\nBUILD=/y\nIMAGE=u-boot.bin\nNAME=u-boot\n", "states NAME"),
            ("SRC=/x\nBUILD=/y\nIMAGE=u-boot.bin\nTYPO=1\n", "not a firmware key"),
            ("SRC=relative\nBUILD=/y\nIMAGE=u-boot.bin\n", "SRC is not an absolute path"),
            ("SRC=/x\nBUILD=/y\nIMAGE=/abs/u-boot.bin\n", "IMAGE is"),
            ("SRC=/x\nBUILD=/y\nIMAGE=../u-boot.bin\n", "IMAGE is"),
            ("SRC=/x\nBUILD=/y\nIMAGE=u-boot.bin\nSYMBOLS=/abs\n", "SYMBOLS is"),
            ("SRC=%s\nBUILD=%s\nIMAGE=u-boot.bin\n" % (root, root), "BUILD is SRC"),
            ("SRC=%s/none\nBUILD=%s/b\nIMAGE=u-boot.bin\n" % (root, root), "is no git checkout"),
        ):
            with open(conf, "w") as f:
                f.write(body)
            expect(env, ["firmware", "own", ARM], 1, word)
            checked += 1
            if os.path.exists(os.path.join(root, "data", "kbuildlab", "trees", ARM, "firmware.registered")):
                failed += 1
                print("FAIL a refused firmware.conf (%s) stayed registered" % word)
        expect(env, ["firmware", ARM], 0, "global firmware for arm64", "is not registered")
        reg = os.path.join(root, "data", "kbuildlab", "trees", ARM, "firmware.registered")
        with open(reg, "w") as f:
            f.write("0" * 64 + "\n")
        expect(env, ["firmware", ARM], 1, "changed since it was registered")
        expect(env, ["dt", ARM], 1, "changed since it was registered")
        os.unlink(conf)
        expect(env, ["firmware", ARM], 1, "is registered and", "is gone")
        expect(env, ["firmware", "global", ARM], 0, "boots kbuildlab's global firmware again")
        expect(env, ["firmware", ARM], 0, "global firmware for arm64")

        for bad, word in (
            (junk, "is not a well-formed one"),
            (empty, "is empty"),
            (os.path.join(root, "missing.dts"), "no readable file"),
            (root, "no readable file"),
        ):
            expect(env, ["dt", "apply", X86, "--boot", "direct", bad], 1)
    print("%d refusals checked, %s" % (checked, "all as they should be" if not failed else "%d failed" % failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
