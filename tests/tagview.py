#!/usr/bin/env python3
import json
import os
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True
here = os.path.dirname(os.path.abspath(__file__))
tagview = os.path.join(os.path.dirname(here), "lib", "index", "tagview.py")
sys.path.insert(0, os.path.dirname(tagview))
import inactive_regions as ir

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


def tree_of(root):
    out = set()
    for d, dirs, files in os.walk(root):
        out |= {os.path.relpath(os.path.join(d, x), root) + "/" for x in dirs}
        out |= {os.path.relpath(os.path.join(d, x), root) for x in files}
    return out


with tempfile.TemporaryDirectory() as tmp:
    src, conf, view = (os.path.join(tmp, x) for x in ("src", "tree", "view"))
    up = os.path.join(view, "upper")
    for rel in ("keep.c", "d/keep.h", "t/inner.c", "u"):
        put(src, rel, "tree\n")
    put(conf, "tree.conf", f"SRC={src}\nARCH=x86_64\n")
    copies = ("keep.c", "d/keep.h", "gone.c", "olddir/x.h", "t", "u/y.h")
    for rel in copies + ("stray.c", ".wh.whited"):
        put(up, rel, "copy\n")
    with open(os.path.join(view, "view.json"), "w") as f:
        json.dump(sorted(copies), f)

    r = subprocess.run([sys.executable, "-B", tagview, "--tree", conf, "--view", view, "harvest", "--drop"],
                       capture_output=True, text=True)
    check("exit status", (r.returncode, r.stderr), (0, ""))
    check("upper", tree_of(up), {"keep.c", "d/", "d/keep.h"})
    with open(os.path.join(view, "view.json")) as f:
        check("view.json", json.load(f), ["d/keep.h", "keep.c"])
    check("report", "dropped the copies of 4 file(s) the tree no longer has" in r.stdout, True)
    check("tree", tree_of(src), {"keep.c", "d/", "d/keep.h", "t/", "t/inner.c", "u"})

    r = subprocess.run([sys.executable, "-B", tagview, "--tree", conf, "--view", view, "harvest", "--drop"],
                       capture_output=True, text=True)
    check("second run", (r.returncode, r.stdout, tree_of(up)), (0, "", {"keep.c", "d/", "d/keep.h"}))

with tempfile.TemporaryDirectory() as tmp:
    put(tmp, "m.h", "#ifdef __ASSEMBLY__\n#define ONLY_A_DEFINE 1\n#endif\n"
                    "#ifndef __ASSEMBLY__\n#define NOT_COMPILED 1\n#endif\n")
    put(tmp, "a.S", '#include "m.h"\n\tnop\n')
    _, rc, err, record = ir.read_unit((0, ir.unit_argv("gcc -D__ASSEMBLY__ -c -o a.o a.S"), tmp))
    check("assembler unit", (rc, err, record["files"].get("m.h")), (0, "", [[0]]))

    shim = os.path.join(os.path.dirname(tagview), "ctags-shim", "ctags")
    put(tmp, "k.h", "#ifdef __ASSEMBLY__\n.macro m\n\tnop\n.endm\n#else\nvoid after_asm(void);\n#endif\n")
    args = [shim, "--langmap=c:+.h", "--c-kinds=+p", "-f", "-", "m.h", "k.h"]
    env = dict(os.environ, KBL_TAGS_PASS="1")
    over_view = subprocess.run(args, cwd=tmp, env=dict(env, KBL_TAGS_STATE=tmp), capture_output=True, text=True)
    plain = subprocess.run(args, cwd=tmp, env=env, capture_output=True, text=True)
    check("ctags over the view", ("ONLY_A_DEFINE" in over_view.stdout, "after_asm" in over_view.stdout,
                                  "m\tk.h\t/^.macro m$/" in over_view.stdout), (True, True, True))
    check("ctags over the tree", ("ONLY_A_DEFINE" in plain.stdout, "after_asm" in plain.stdout), (False, True))

with tempfile.TemporaryDirectory() as tmp:
    put(tmp, "m.h", "#ifdef A\n#define TWICE(x) x\n.macro both\n#else\n#define TWICE(x) #x\n.macro both\n#endif\n"
                    "int once;\nint once;\n")
    put(tmp, "tags", '!_TAG_X\t1\t//\nTWICE\tm.h\t/^#define TWICE(/;"\td\tsignature:(x)\n'
                     'both\tm.h\t/^.macro both$/;"\tA\nonce\tm.h\t/^int once;$/;"\tv\n')
    r = subprocess.run([sys.executable, "-B", tagview, "address"], cwd=tmp, capture_output=True, text=True)
    with open(os.path.join(tmp, "tags")) as f:
        check("address", (r.returncode, f.read()),
              (0, '!_TAG_X\t1\t//\nTWICE\tm.h\t2;"\td\tsignature:(x)\nTWICE\tm.h\t5;"\td\tsignature:(x)\n'
                  'both\tm.h\t3;"\tA\nboth\tm.h\t6;"\tA\nonce\tm.h\t8;"\tv\n'))

print("tagview: the view kept only copies of files the tree has; assembler-only #define lines reach the indexes"
      if not failed else f"tagview: {failed} failed")
sys.exit(1 if failed else 0)
