#!/usr/bin/env python3
import os
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True
here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(here), "lib", "index"))
import build_outputs as bo

failed = 0


def check(name, got, want):
    global failed
    if got != want:
        failed += 1
        print(f"FAIL {name}: want {want!r}, got {got!r}")


def put(root, rel, text=""):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def record(root, target, command):
    d, base = os.path.split(target)
    put(root, os.path.join(d, f".{base}.cmd"), f"savedcmd_{target} := {command}\n\nsource_{target} := x\n")
    put(root, target, "output\n")


def thin(root, rel, members):
    names = "".join(m + "/\n" for m in members)
    header = "//".ljust(48) + str(len(names)).ljust(10) + "`\n"
    with open(os.path.join(root, rel), "w") as f:
        f.write("!<thin>\n" + header + names)


with tempfile.TemporaryDirectory() as k:
    record(k, "a/live.o", "gcc -c -o a/live.o a/live.c")
    record(k, "a/off.o", "gcc -c -o a/off.o a/off.c")
    record(k, "b/part.o", "gcc -c -o b/part.o b/part.c")
    record(k, "b/whole.o", "ld -r -o b/whole.o @b/whole.mod")
    record(k, "b/whole.mod", "printf b/part.o")
    put(k, "b/whole.mod", "b/part.o\n")
    record(k, "m/mod.o", "gcc -c -o m/mod.o m/mod.c")
    record(k, "m/mod.mod.o", "gcc -c -o m/mod.mod.o m/mod.mod.c")
    record(k, "m/mod.ko", "ld -r -o m/mod.ko m/mod.o m/mod.mod.o")
    record(k, "m/gone.o", "gcc -c -o m/gone.o m/gone.c")
    record(k, "m/gone.mod.o", "gcc -c -o m/gone.mod.o m/gone.mod.c")
    record(k, "m/gone.ko", "ld -r -o m/gone.ko m/gone.o m/gone.mod.o")
    record(k, "boot/entry.o", "gcc -c -o boot/entry.o boot/entry.S")
    record(k, "boot/image", "ld -o boot/image boot/entry.o")
    record(k, "linked.o", "gcc -c -o linked.o linked.c")
    record(k, "vmlinux", 'sh scripts/link.sh "ld"')
    put(k, "scripts/link.sh", "ld -o vmlinux linked.o\n")
    put(k, "modules.order", "m/mod.o\n")
    put(k, "tools/x/.other.o.cmd", f"cmd_{k}/tools/x/other.o := gcc\n")
    put(k, "tools/x/other.o", "output\n")
    thin(k, "vmlinux.a", ["a/live.o", "b/whole.o"])
    record(k, "old/sub.o", "gcc -c -o old/sub.o old/sub.c")
    record(k, "old/sub.so", "ld -o old/sub.so old/sub.o")
    record(k, "old/image.c", "gen old/sub.so > old/image.c")
    record(k, "old/image.o", "gcc -c -o old/image.o old/image.c")
    want = ["a/off.o", "m/gone.ko", "m/gone.mod.o", "m/gone.o", "old/image.o", "old/sub.o"]
    check("stale outputs, also under a stale product", bo.stale_outputs(k), want)

    for t in want:
        bo.forget(k, t)
    check("forgotten with their records",
          [os.path.exists(os.path.join(k, p)) for p in ("a/off.o", "a/.off.o.cmd", "a/live.o", "a/.live.o.cmd")],
          [False, False, True, True])
    check("nothing else is stale", bo.stale_outputs(k), [])

    os.unlink(os.path.join(k, "vmlinux.a"))
    check("no thin archive, no judgement", bo.stale_outputs(k), [])
    with open(os.path.join(k, "vmlinux.a"), "w") as f:
        f.write("!<arch>\n")
    check("a normal archive, no judgement", bo.stale_outputs(k), [])
    thin(k, "vmlinux.a", [])
    put(k, "modules.order", "")
    check("most of a build being stale is a wrong reading", bo.stale_outputs(k), [])

with tempfile.TemporaryDirectory() as k:
    subprocess.run(["git", "init", "-q", k], check=True)
    put(k, ".gitignore", "*.o\n*.a\n.*.cmd\ninclude/generated/\n*.patch\n")
    put(k, "a/tracked.c", "int x;\n")
    put(k, "a/tracked.h", "int y;\n")
    put(k, "a/forced.o", "kept in git\n")
    put(k, "a/mine.patch", "not an output\n")
    subprocess.run(["git", "-C", k, "add", "-f", "a/tracked.c", "a/tracked.h", "a/forced.o"], check=True)
    put(k, "include/generated/foo.h", "#error stale\n")
    record(k, "a/bad.o", "gcc -c -o a/bad.o a/bad.c")
    record(k, "a/beside.o", "gcc -c -o a/beside.o a/beside.c")
    record(k, "b/linked.o", "gcc -c -o b/linked.o b/linked.c")
    log = """  CC      a/fine.o
./include/generated/foo.h:1:2: error: #error stale
a/tracked.c:1:5: error: unknown type name
In file included from a/tracked.h:1:
make[3]: *** [scripts/Makefile.build:287: a/bad.o] Error 1
make[2]: *** [scripts/Makefile.build:544: a] Error 2
make[1]: *** No rule to make target 'a/old.h', needed by 'a/forced.o'.  Stop.
/usr/bin/aarch64-linux-gnu-ld.bfd: vmlinux.a: member b/linked.o in archive is not an object
"""
    check("what a failure names, generated only", bo.failed_outputs(k, log), ["a/bad.o", "b/linked.o", "include/generated/foo.h"])
    check("what lies beside it, recorded only", bo.failed_directories(k, log), ["a/bad.o", "a/beside.o"])
    record(k, "top.o", "gcc -c -o top.o top.c")
    os.makedirs(os.path.join(k, "a/sub"), exist_ok=True)
    only_above = "make[2]: *** [scripts/Makefile.build:544: a/sub] Error 2\n"
    check("a failed directory says nothing of its parent", bo.failed_directories(k, only_above), [])
    warned = "b/linked.o: warning: objtool: something\nld: warning: a/bad.o: missing .note.GNU-stack section\n"
    check("a warning names no failure", bo.failed_outputs(k, warned), [])

with tempfile.TemporaryDirectory() as k:
    put(k, "a/bad.o", "x\n")
    log = "make[3]: *** [scripts/Makefile.build:287: a/bad.o] Error 1\n"
    check("no git, nothing given up", bo.failed_outputs(k, log), [])

print("build-outputs: stale outputs and what a failure names are told apart" if not failed else f"build-outputs: {failed} failed")
sys.exit(1 if failed else 0)
