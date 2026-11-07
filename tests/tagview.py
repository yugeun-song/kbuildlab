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

with tempfile.TemporaryDirectory() as tmp:
    merge = os.path.join(os.path.dirname(tagview), "tags-merge.sh")
    state = os.path.join(tmp, "state")
    os.mkdir(state)
    names = [f"f{i}.c" for i in range(9)]
    for i, name in enumerate(names):
        put(tmp, name, f"int one_{i};\n")
    opts = ["-a", "--c-kinds=+p"]
    put(state, "keys", "k\n")
    put(state, "opts.k", "\0".join(opts) + "\0")
    put(state, "files.k", "".join(n + "\n" for n in names))
    env = dict(os.environ, KBL_TAGS_STATE=state, KBL_JOBS="3")

    def single():
        out = os.path.join(state, "single")
        if os.path.exists(out):
            os.unlink(out)
        subprocess.run(["ctags", *opts, "-f", out, *names], cwd=tmp, check=True, capture_output=True)
        with open(out) as f:
            return f.read()

    def workers():
        r = subprocess.run(["bash", merge], cwd=tmp, env=env, capture_output=True, text=True)
        with open(os.path.join(state, "tags.new")) as f:
            return r.returncode, f.read(), [x for x in os.listdir(state) if x.startswith("part.")]

    put(state, "tags.prev")
    check("ctags workers, everything", workers(), (0, single(), []))
    os.replace(os.path.join(state, "tags.new"), os.path.join(state, "tags.prev"))
    put(tmp, names[4], "int two_4;\n")
    newer = os.path.getmtime(os.path.join(state, "tags.prev")) + 60
    os.utime(os.path.join(tmp, names[4]), (newer, newer))
    check("ctags workers, one file changed", workers(), (0, single(), []))

with tempfile.TemporaryDirectory() as tmp:
    state = os.path.join(tmp, "state")
    tree, tools = os.path.join(tmp, "tree"), os.path.join(tmp, "tools")
    put(tree, "a.c", "int one;\n")
    put(tree, "tags", "published\n")
    put(tmp, "published", "")
    os.unlink(os.path.join(tmp, "published"))
    os.link(os.path.join(tree, "tags"), os.path.join(tmp, "published"))
    os.mkdir(state)
    os.link(os.path.join(tree, "tags"), os.path.join(state, "tags.prev"))
    put(state, "keys", "k\n")
    put(state, "tags.keys", "k\n")
    put(state, "opts.k", "-a\0")
    put(state, "files.k", "a.c\n")
    put(tools, "make", "#!/bin/sh\nexit 1\n")
    os.chmod(os.path.join(tools, "make"), 0o755)
    r = subprocess.run(["bash", os.path.join(os.path.dirname(tagview), "tags-index.sh"), "x86", "full"], cwd=tree,
                       env=dict(os.environ, KBL_TAGS_STATE=state, KBL_JOBS="2",
                                PATH=tools + os.pathsep + os.environ["PATH"]), capture_output=True, text=True)
    with open(os.path.join(tmp, "published")) as f:
        check("a failed full run keeps the published tags", (r.returncode, f.read()), (1, "published\n"))

with tempfile.TemporaryDirectory() as tmp:
    state = os.path.join(tmp, "state")
    tree, tools = os.path.join(tmp, "tree"), os.path.join(tmp, "tools")
    put(tree, "a.c", "int one;\n")
    os.mkdir(state)
    put(state, "keys", "k\n")
    put(state, "tags.keys", "k\n")
    put(state, "opts.k", "-a\0")
    put(state, "files.k", "a.c\n")
    put(state, "cscope.ran")
    put(tools, "make", '#!/bin/sh\ncase "$*" in\n*cscope*) [ -e "$KBL_TAGS_STATE/cscope.again" ] && exit 0\n'
                       ': > "$KBL_TAGS_STATE/cscope.again"; exit 1 ;;\n*gtags*) exit 0 ;;\nesac\n: > tags\n')
    os.chmod(os.path.join(tools, "make"), 0o755)
    r = subprocess.run(["bash", os.path.join(os.path.dirname(tagview), "tags-index.sh"), "x86", "full"], cwd=tree,
                       env=dict(os.environ, KBL_TAGS_STATE=state, KBL_JOBS="2",
                                PATH=tools + os.pathsep + os.environ["PATH"]), capture_output=True, text=True)
    check("one failed index is built again on its own",
          (r.returncode, "cscope failed; indexing it again" in r.stderr, "gtags failed" in r.stderr), (0, True, False))

with tempfile.TemporaryDirectory() as tmp:
    sys.path.insert(0, os.path.dirname(tagview))
    import build_symbols as bs

    expanded = """# 1 "a.S"
# 1 "<built-in>"
#define FROM_COMMAND 1
# 5 "a.S"
#define HERE 2
real: other: .long 1
1: inner:
.macro maker name, other
\\name\\():
 nop
.endm
.macro user target
 b \\target
.endm
.macro relay x
 maker \\x, 1
.endm
 maker made_by_macro, 1
 user only_used
 relay made_through
 maker key=value, name=made_by_key
src .req x1
count = 3
.set assigned, 4
.comm shared, 8
not_a_symbol:
"""
    syms = {"real", "other", "inner", "made_by_macro", "only_used", "made_through", "made_by_key", "shared"}
    got = {(n, line, form) for n, place, line, form in bs.assembler_definitions(expanded, syms) if place == "a.S"}
    want = {("HERE", 5, "define"), ("real", 6, "symbol"), ("other", 6, "symbol"), ("inner", 7, "symbol"),
            ("maker", 8, "macro"), ("user", 12, "macro"), ("relay", 15, "macro"),
            ("made_by_macro", 18, "symbol"), ("made_through", 20, "symbol"), ("made_by_key", 21, "symbol"),
            ("src", 22, "define"), ("count", 23, "define"), ("assigned", 24, "define"), ("shared", 25, "symbol")}
    check("assembler definitions", sorted(got), sorted(want))

    state = os.path.join(tmp, "state")
    os.mkdir(state)
    put(tmp, "gen.h", "#define MAKE(n) int n##_made __attribute__((used))\n")
    put(tmp, "a.c", '#include "gen.h"\n\nMAKE(foo);\nint plain;\n__attribute__((aligned(16)))\nint\nby_attr;\n'
                    "static void quiet(void) { }\nvoid (*keep)(void) = quiet;\n/* by_asm */\n"
                    '__asm__(".globl by_asm; .set by_asm, 3");\n')
    put(tmp, "b.S", "#define DEF(x) .globl x; x:\nDEF(by_call)\n  nop\n.macro maker name\n\\name:\n .long 1\n.endm\n"
                    "maker by_macro\n")
    put(tmp, "tags", '!_TAG_FILE_SORTED\t1\t//\nkeep\ta.c\t9;"\tv\nold\tb.S\t1;"\tv\torigin:build\n'
                     'plain\ta.c\t4;"\tv\n')
    for src, obj, flags in (("a.c", "a.o", "-g -gdwarf-5"), ("b.S", "b.o", "-g")):
        cmd = f"gcc {flags} -c -o {obj} {src}"
        r = subprocess.run(cmd.split(), cwd=tmp, capture_output=True, text=True)
        check(f"builds {src}", (r.returncode, r.stderr), (0, ""))
        put(tmp, f".{obj}.cmd", f"savedcmd_{obj} := {cmd}\n\nsource_{obj} := {src}\n\ndeps_{obj} := \\\n  gen.h \\\n\n"
                                f"{obj}: $(deps_{obj})\n\n$(deps_{obj}):\n")
    put(state, "keys", "k\n")
    put(state, "files.k", "a.c\nb.S\ngen.h\n")
    script = os.path.join(os.path.dirname(tagview), "build_symbols.py")

    def run_symbols():
        return subprocess.run([sys.executable, "-B", script], cwd=tmp, capture_output=True, text=True,
                              env=dict(os.environ, KBL_TAGS_STATE=state, KBL_JOBS="2"))

    def added():
        with open(os.path.join(tmp, "tags")) as f:
            lines = f.read().split("\n")
        check("tags stay sorted", [x for x in lines if x] == sorted(x for x in lines if x), True)
        return {tuple(x.split("\t")[:2]) + (x.split("\t")[2].split(";")[0],) for x in lines if x.endswith("origin:build")}

    r = run_symbols()
    check("symbols run", (r.returncode, r.stderr), (0, ""))
    want = {("foo_made", "a.c", "3"), ("by_attr", "a.c", "7"), ("quiet", "a.c", "8"), ("by_call", "b.S", "2"),
            ("by_macro", "b.S", "8"), ("DEF", "b.S", "1"), ("maker", "b.S", "4"), ("by_asm", "a.c", "11")}
    check("definitions ctags did not see", sorted(added()), sorted(want))
    with open(os.path.join(tmp, "tags")) as f:
        text = f.read()
    check("what ctags saw stays, the last run's guesses go",
          ('plain\ta.c\t4;"\tv\n' in text, "\nold\t" in text, 'quiet\ta.c\t8;"\tf\tfile:\torigin:build\n' in text),
          (True, False, True))
    r = run_symbols()
    check("nothing changed, nothing read", (r.returncode, "2 objects (2 carried over)" in r.stdout, sorted(added())),
          (0, True, sorted(want)))

    made_by = subprocess.run(["ctags", "--extras=+p", "-o", "-", os.devnull], cwd=tmp, capture_output=True, text=True)
    put(state, "tags.prev", "".join(x + "\n" for x in made_by.stdout.split("\n") if x.startswith("!_TAG_"))
                            + 'keep\ta.c\t9;"\tv\nquiet\ta.c\t8;"\tf\tfile:\torigin:build\n')
    put(state, "opts.k", "-a\0")
    merged = subprocess.run(["bash", os.path.join(os.path.dirname(tagview), "tags-merge.sh")], cwd=tmp,
                            env=dict(os.environ, KBL_TAGS_STATE=state, KBL_JOBS="1"), capture_output=True, text=True)
    with open(os.path.join(state, "tags.new")) as f:
        check("a merge leaves the build's lines out", (merged.returncode, "origin:build" in f.read()), (0, False))

print("tagview: the view kept only copies of files the tree has; assembler-only #define lines reach the indexes"
      if not failed else f"tagview: {failed} failed")
sys.exit(1 if failed else 0)
