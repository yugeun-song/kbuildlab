#!/usr/bin/env python3
import json
import os
import subprocess
import sys
import tempfile

here = os.path.dirname(os.path.abspath(__file__))
tagview = os.path.join(os.path.dirname(here), "lib", "index", "tagview.py")

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

print("tagview: the view kept only copies of files the tree has" if not failed else f"tagview: {failed} failed")
sys.exit(1 if failed else 0)
