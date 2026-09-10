#!/usr/bin/env python3
"""Make clangd read a GCC-built kernel tree without inventing errors.

Run by `kbuildlab tags`, alongside ctags and cscope: the three answer the same
question -- where is this symbol -- and a tree that has been reindexed for two
of them and not the third is a tree whose editor is quietly out of date.

Nothing is written inside a kernel tree.  The compilation database lives in
$XDG_CACHE_HOME/kbuildlab/clangd/<tree>/, and clangd is pointed at it from the
user config with a per-tree PathMatch, so `git status` in a tree stays empty,
kbuild sees no new files, and sync/update never has to know this exists.
clangd's background index follows the database rather than the source, so it
lands in the same cache directory.  Each tree owns one marked block in the
config, so reindexing one tree leaves the others exactly as they were.

Six things break clangd on a GCC-built kernel tree.  Each is fixed by
measuring rather than by keeping a list, so a tree that is rebuilt, retargeted
or replaced does not need this file edited:

  1. No compilation database.  Without one clangd guesses the include paths and
     every <linux/...> resolves to nothing.  `gen` builds compile_commands.json
     out of the .cmd files kbuild already wrote, so no rebuild is needed.
     Assembly entries are dropped: clangd cannot parse them, and leaving them
     in makes every nearby header inherit -D__ASSEMBLY__.

  2. GCC-only flags.  A kernel command line carries options clang has never had
     (-fconserve-stack, -mabi=lp64 on arm64, -mindirect-branch=...).  clang
     answers each with a driver error, and one of them -- an ABI or CPU name it
     does not know -- kills the parse outright, so the file shows as a wall of
     red.  Every unique flag in the database is probed against the real clang
     for that tree's target, and only what clang rejects is removed.  Probing
     is what keeps this right across trees: -mabi=lp64 is wrong on arm64 and
     correct on riscv64, and nobody has to remember that.

  3. Sub-builds aimed at another machine.  x86_64 compiles its boot stub and
     realmode trampoline as i386, riscv64 builds a 32-bit compat vDSO.  Those
     files get a fragment naming their real target, which beats deleting the
     flags: the code then parses as what it actually is.

  4. Headers parsed on their own.  A kernel header is not self-contained --
     open asm/atomic_lse.h and atomic_t is an unknown type, because in a real
     build asm/atomic.h got there first.  Each header gets its own database
     entry reproducing the includes that precede it in a source that uses it,
     which is what makes hover and go-to-definition work inside a header.  On
     a 87-header sample this took the diagnostics from 2296 to 4.

  5. Source this tree never compiles.  A tree configured for arm64 does not
     build arch/powerpc, and no command line makes powerpc source parse through
     an aarch64 one.  Same for tools/ and samples/, which are separate builds.
     Those paths keep navigation and lose diagnostics.

  6. clang and GCC disagreeing about their own headers.  clang's arm_neon.h
     types uint64_t as unsigned long long where GCC's uses unsigned long, so a
     NEON routine reads as a wall of pointer-type errors about a header the
     kernel never wrote.  Files that reach an intrinsic header, directly or
     through an arch wrapper, get exactly the suppressions that difference
     needs.

Everything named "unused" is off: the compiler's -Wunused family, clangd's own
unused-include signals, and the clang-tidy checks for unused parameters and
declarations.

`check` measures the result the way an editor sees it: a real clangd LSP
session, didOpen per file, counting published diagnostics.  clangd --check is
not the verdict -- it reports errors only and stays silent about every warning
that still lands in the editor's gutter.  It is used only as a second opinion,
for files clangd publishes nothing about at all.

Usage (kbuildlab supplies the paths; --peer lets a tree too old to ship
scripts/clang-tools/gen_compile_commands.py borrow one from a newer tree):
    clangd.py --tree DIR [--peer DIR ...] gen [-f]
    clangd.py --tree DIR check [--all] [-n N] [-j N] [--journal PREFIX]
    clangd.py --tree DIR clean
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import queue
import random
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kbuildinfo

CACHE = os.path.join(os.environ.get("XDG_CACHE_HOME",
                                    os.path.expanduser("~/.cache")),
                     "kbuildlab", "clangd")
USER_CONFIG = os.path.join(os.environ.get("XDG_CONFIG_HOME",
                                          os.path.expanduser("~/.config")),
                           "clangd", "config.yaml")

# One marked block per tree, so `kbuildlab tags <one-tree>` rewrites that tree's
# fragments and leaves the other six exactly as they were.
def block_markers(tree: str) -> tuple[str, str]:
    return (f"# >>> kbuildlab clangd: {tree} >>>",
            f"# <<< kbuildlab clangd: {tree} <<<")


# Trees this invocation knows about.  Registered from the command line rather
# than searched for: kbuildlab hands its tools a workspace and a tree, and a
# tool that goes looking is how symbols end up attached to the wrong machine.
TREES: dict[str, dict] = {}


def register_tree(tree_dir: str) -> str | None:
    """Record a tree directory.  Returns its name, or None if it is not one."""
    tree_dir = os.path.abspath(tree_dir)
    if not os.path.isfile(os.path.join(tree_dir, "tree.conf")):
        return None
    name = os.path.basename(tree_dir.rstrip("/"))
    src = tree_source(tree_dir)
    if src is None:
        return None
    TREES[name] = {"dir": tree_dir, "src": src, "arch": tree_conf_arch(tree_dir)}
    return name


def is_kernel_source(d: str) -> bool:
    """The same fingerprint kbuildlab's kbl_is_ksrc uses: content, not name."""
    if not (os.path.isdir(os.path.join(d, "arch"))
            and os.path.isfile(os.path.join(d, "Kbuild"))
            and os.path.isfile(os.path.join(d, "Makefile"))):
        return False
    try:
        with open(os.path.join(d, "Makefile"), encoding="utf-8", errors="replace") as f:
            head = f.read(4096)
    except OSError:
        return False
    return all(re.search(rf"^\s*{k}\s*=", head, re.M)
               for k in ("VERSION", "PATCHLEVEL", "SUBLEVEL"))


def tree_source(tree_dir: str) -> str | None:
    named = tree_conf_get(tree_dir, "SRC_DIR")
    if named:
        cand = os.path.join(tree_dir, named)
        return cand if os.path.isdir(cand) else None
    found = [os.path.join(tree_dir, d) for d in sorted(os.listdir(tree_dir))
             if is_kernel_source(os.path.join(tree_dir, d))]
    if len(found) == 1:
        return found[0]
    cand = os.path.join(tree_dir, "kernel")
    return cand if os.path.isdir(cand) else None


def tree_conf_get(tree_dir: str, key: str) -> str | None:
    path = os.path.join(tree_dir, "tree.conf")
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line.startswith(f"{key}="):
                    return line.partition("=")[2].strip()
    except OSError:
        pass
    return None


def tree_conf_arch(tree_dir: str) -> str | None:
    return tree_conf_get(tree_dir, "ARCH")


def discover_trees() -> list[str]:
    return sorted(TREES)


def kernel_root(tree: str) -> str:
    return TREES[tree]["src"]


def cache_dir(tree: str) -> str:
    return os.path.join(CACHE, tree)


# ---------------------------------------------------------------------------
# compile_commands.json
# ---------------------------------------------------------------------------

GEN_REL = "scripts/clang-tools/gen_compile_commands.py"


def find_generator(tree: str) -> str:
    """kbuild's own generator, present since v5.4.  Older trees borrow it from
    a newer one in the lab -- the .cmd format it reads has not changed, and
    copying it into the old tree would be writing into the source."""
    own = os.path.join(kernel_root(tree), GEN_REL)
    if os.path.isfile(own):
        return own
    for other in discover_trees():
        cand = os.path.join(kernel_root(other), GEN_REL)
        if os.path.isfile(cand):
            return cand
    raise SystemExit("no gen_compile_commands.py anywhere in the lab")


def gen_cdb(tree: str, force: bool) -> tuple[int, int]:
    ksrc = kernel_root(tree)
    out_dir = cache_dir(tree)
    os.makedirs(out_dir, exist_ok=True)
    cdb = os.path.join(out_dir, "compile_commands.json")
    if os.path.exists(cdb) and not force:
        with open(cdb) as f:
            return len(json.load(f)), 0

    gen = find_generator(tree)
    tmp = cdb + ".tmp"
    subprocess.run([sys.executable, gen, "-d", ksrc, "-o", tmp],
                   cwd=ksrc, check=True, capture_output=True, text=True)
    with open(tmp) as f:
        entries = json.load(f)
    os.unlink(tmp)

    # Assembly is not a language clangd parses.  Left in, these entries also
    # poison headers: clangd infers a header's command from whatever database
    # entry sits nearest it, and an .S command carries -D__ASSEMBLY__ and drops
    # the -include compiler_types.h every header needs.  On an arm64 tree that
    # alone turns asm/atomic_lse.h into a screen of errors.
    kept = [e for e in entries if not e["file"].endswith(".S")]
    with open(cdb, "w") as f:
        json.dump(kept, f, indent=1)
    return len(kept), len(entries) - len(kept)


def cdb_entries(tree: str) -> list[dict]:
    with open(os.path.join(cache_dir(tree), "compile_commands.json")) as f:
        return json.load(f)


def overlay_dir(tree: str) -> str:
    return os.path.join(cache_dir(tree), "overlay")


def apply_overlay(tree: str) -> int:
    """Put the annotated headers ahead of the tree's own on the include path.

    The overlay holds byte-identical copies of two of the tree's headers with
    build-system knowledge added as comments (see kbuildinfo.py).  -I is
    searched in order, so the entry has to go in front of the kernel's own -I
    rather than being appended -- which is why this edits the database instead
    of using the config's CompileFlags.Add, where everything lands at the end.
    """
    ov = overlay_dir(tree)
    if not os.path.isdir(ov):
        return 0
    cdb = os.path.join(cache_dir(tree), "compile_commands.json")
    with open(cdb) as f:
        entries = json.load(f)
    flag = f"-I{ov}"

    # The build names its forced includes by path, not by search: -include
    # ./include/linux/compiler_types.h reaches the tree's copy whatever -I says,
    # and the guard then keeps the overlay's copy from ever being read.  So the
    # path itself is rewritten for the headers the overlay actually replaces.
    redirect = {}
    for rel in ("linux/compiler_types.h",):
        if os.path.isfile(os.path.join(ov, rel)):
            redirect["./include/" + rel] = os.path.join(ov, rel)
            redirect["include/" + rel] = os.path.join(ov, rel)

    def fix(args: list[str]) -> bool:
        did = False
        for i, a in enumerate(args):
            if a == "-include" and i + 1 < len(args) and args[i + 1] in redirect:
                args[i + 1] = redirect[args[i + 1]]
                did = True
        return did

    touched = 0
    for e in entries:
        cmd = e.get("command")
        if cmd is not None:
            args = shlex.split(cmd)
            changed = fix(args)
            if flag not in args:
                args.insert(1, flag)
                changed = True
            if not changed:
                continue
            e["command"] = shlex.join(args)
        else:
            changed = fix(e["arguments"])
            if flag not in e["arguments"]:
                e["arguments"].insert(1, flag)
                changed = True
            if not changed:
                continue
        touched += 1
    if touched:
        with open(cdb, "w") as f:
            json.dump(entries, f, indent=1)
    return touched


def entry_args(e: dict) -> list[str]:
    cmd = e.get("command")
    return shlex.split(cmd) if cmd else list(e["arguments"])


_INCLUDE_RE = re.compile(r'^[ \t]*#[ \t]*include[ \t]*[<"]([^>"]+)[>"]', re.M)

HEADER_SUFFIXES = (".h", ".hpp", ".hh", ".hxx", ".inc")


def is_header(path: str) -> bool:
    return path.endswith(HEADER_SUFFIXES)


def header_entries(tree: str, entries: list[dict], verbose: bool) -> list[dict]:
    """Give each header a command that reflects how the tree actually includes it.

    A kernel header is not self-contained.  scsi/fc_encode.h names fc_frame and
    fc_lport without including anything that declares them, because the one
    file that includes it -- drivers/scsi/libfc/fc_lport.c -- has already
    included scsi/libfc.h four lines earlier.  Opened on its own it is several
    hundred errors, and clangd's guess of a neighbouring file's command does
    not help, because the problem is ordering rather than flags.

    So the ordering is reconstructed: for each header, find a source that
    includes it, take the includes that came before it in that source, and put
    them in front with -include.  That is the same prefix a real build sees, so
    the header parses the way it was written to be parsed -- which fixes the
    diagnostics and, more to the point, makes hover and go-to-definition inside
    a header work on real types instead of on wreckage.
    """
    ksrc = kernel_root(tree)
    by_source: dict[str, dict] = {os.path.relpath(e["file"], ksrc): e for e in entries}

    # How each file includes things, in order.  Read once; the kernel is large
    # but this is a single linear pass over the files already in the database
    # plus the headers they reach.
    inc_cache: dict[str, list[str] | None] = {}

    def includes_of(rel: str) -> list[str] | None:
        if rel in inc_cache:
            return inc_cache[rel]
        try:
            with open(os.path.join(ksrc, rel), encoding="utf-8", errors="replace") as f:
                got = _INCLUDE_RE.findall(f.read())
        except OSError:
            got = None
        inc_cache[rel] = got
        return got

    # Resolve an include spelling to a tree-relative path, using the same
    # directories the kernel's own -I list names.
    search = ["include", f"arch/{srcarch(tree) or ''}/include",
              f"arch/{srcarch(tree) or ''}/include/generated", "include/uapi",
              f"arch/{srcarch(tree) or ''}/include/uapi", "include/generated/uapi",
              "include/generated", "."]

    resolved: dict[str, str | None] = {}

    def resolve(spelling: str) -> str | None:
        if spelling in resolved:
            return resolved[spelling]
        hit = None
        for d in search:
            cand = os.path.normpath(os.path.join(d, spelling))
            if os.path.isfile(os.path.join(ksrc, cand)):
                hit = cand
                break
        resolved[spelling] = hit
        return hit

    # For every source in the database, walk its include list and record, for
    # each header, the includes that preceded it.  The richest context wins
    # rather than the first one found: a driver that opens with <linux/pci.h>
    # supplies nothing, while one that reaches it after twenty other headers
    # supplies the declarations pci.h was written to assume.  Long prefixes are
    # capped, because past a couple of dozen headers the preamble costs more
    # than the extra context is worth.
    # Measured, not guessed: cutting the prefix to 12 put pte_t back out of
    # reach in asm-generic/pgtable_uffd.h and took the sample from 4 stray
    # diagnostics to 27.  A header that includes nothing itself -- a
    # dt-bindings constant file, say -- is a different case: it needs no
    # prefix, and giving it one only makes opening it slow.
    MAX_PREFIX = 24
    context: dict[str, tuple[str, list[str]]] = {}

    def offer(target: str, source: str, prefix: list[str]) -> None:
        cur = context.get(target)
        if cur is None or len(prefix) > len(cur[1]):
            context[target] = (source, prefix[-MAX_PREFIX:])

    for rel in by_source:
        incs = includes_of(rel)
        if not incs:
            continue
        prefix: list[str] = []
        for spelling in incs:
            target = resolve(spelling)
            if target:
                offer(target, rel, list(prefix))
            prefix.append(spelling)

    # Headers reached only through other headers inherit the including header's
    # context, extended by whatever it included first.  A few passes settle the
    # kernel's include graph, which is shallow once the sources are accounted
    # for; each pass can only lengthen a prefix, so this converges.
    for _ in range(4):
        before = sum(len(p) for _, p in context.values())
        for header, (source, prefix) in list(context.items()):
            incs = includes_of(header)
            if not incs:
                continue
            inner: list[str] = []
            for spelling in incs:
                target = resolve(spelling)
                if target and target != header:
                    offer(target, source, prefix + inner)
                inner.append(spelling)
        if sum(len(p) for _, p in context.values()) == before:
            break

    # A prefix header that itself pulls in the target defeats the whole idea.
    # clangd deliberately ignores a header's own include guard when that header
    # is the file being edited -- otherwise opening one would show an empty
    # buffer -- so a header already dragged in by the preamble gets parsed a
    # second time as the main file, and every typedef and static inline in it
    # is a redefinition.  asm/pgtable.h hits this through linux/mm.h.  So the
    # transitive include set of each candidate is computed, and anything that
    # reaches the target is dropped from its prefix.
    # Transitive reachability over the include graph, computed as a fixed point
    # with one bitset per node.  A depth-limited DFS is not enough here: kernel
    # headers include each other in cycles, so a memoised walk caches whatever
    # partial answer the cycle cut off, and the one prefix entry that does
    # reach the target survives.  Iterating to a fixed point has no such hole.
    nodes: list[str] = []
    index: dict[str, int] = {}

    def node_id(rel: str) -> int:
        i = index.get(rel)
        if i is None:
            i = len(nodes)
            index[rel] = i
            nodes.append(rel)
        return i

    for rel in list(by_source) + list(context):
        node_id(rel)
    direct: list[list[int]] = []
    seen_nodes = 0
    while seen_nodes < len(nodes):
        rel = nodes[seen_nodes]
        seen_nodes += 1
        edges = []
        for spelling in includes_of(rel) or ():
            target = resolve(spelling)
            if target:
                edges.append(node_id(target))
        direct.append(edges)
    reach = [0] * len(nodes)
    converged = False
    for _ in range(200):
        changed = False
        for i in range(len(nodes) - 1, -1, -1):
            bits = reach[i]
            for j in direct[i]:
                bits |= (1 << j) | reach[j]
            if bits != reach[i]:
                reach[i] = bits
                changed = True
        if not changed:
            converged = True
            break
    if not converged and verbose:
        print("    NOTE: include reachability did not converge; "
              "prefixes may keep a self-including header")

    def reaches(rel: str, target: str) -> bool:
        i, j = index.get(rel), index.get(target)
        if i is None or j is None:
            return False
        return bool(reach[i] >> j & 1)

    # A header that is only #define lines needs no context at all -- the
    # dt-bindings constant files are the whole class -- and handing one a
    # 23-header preamble is how opening it comes to take minutes.  "Includes
    # nothing" is NOT the test: asm-generic/pgtable_uffd.h includes nothing
    # and still says pte_t on its first line.  The test is whether the file
    # contains anything but preprocessor directives.
    macro_only: dict[str, bool] = {}

    def pure_macro(rel: str) -> bool:
        if rel in macro_only:
            return macro_only[rel]
        verdict = False
        try:
            with open(os.path.join(ksrc, rel), encoding="utf-8", errors="replace") as f:
                text = f.read()
            body = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
            body = re.sub(r"//[^\n]*", "", body)
            verdict = not any(line.strip() and not line.lstrip().startswith("#")
                              for line in body.splitlines())
        except OSError:
            verdict = False
        macro_only[rel] = verdict
        return verdict

    made = []
    dropped_self = 0
    for header, (source, prefix) in context.items():
        base = by_source.get(source)
        if base is None:
            continue
        clean: list[str] = []
        for spelling in prefix:
            target = resolve(spelling)
            if target == header or (target and reaches(target, header)):
                dropped_self += 1
                continue
            clean.append(spelling)
        prefix = clean
        args = entry_args(base)
        # Drop the source's own output and dependency bookkeeping, then state
        # the prefix and the header itself.
        out: list[str] = []
        i = 0
        while i < len(args):
            a = args[i]
            if a in ("-o", "-MT", "-MF"):
                i += 2
                continue
            if a in ("-c",) or a.startswith("-Wp,-M"):
                i += 1
                continue
            # The build forces three headers in front of every file.  When one
            # of those three IS the file being opened, forcing it produces the
            # same double parse, so it is dropped for that one entry.
            if a == "-include" and i + 1 < len(args):
                spelled = args[i + 1]
                forced = resolve(spelled.lstrip("./")) or os.path.normpath(
                    os.path.relpath(os.path.join(ksrc, spelled), ksrc))
                if forced == header or reaches(forced, header):
                    dropped_self += 1
                    i += 2
                    continue
            if a == args[-1] and a.endswith((".c", ".S")):
                i += 1
                continue
            out.append(a)
            i += 1
        # A header nothing includes before anything else still needs a floor of
        # context; linux/kernel.h is what most of the tree assumes is present.
        chain = [] if pure_macro(header) else (prefix or ["linux/kernel.h"])
        for spelling in chain:
            if spelling != header and resolve(spelling):
                out += ["-include", spelling]
        out += ["-x", "c-header", header]
        made.append({"directory": ksrc, "file": os.path.join(ksrc, header),
                     "command": shlex.join(out)})
    if verbose:
        print(f"    {len(made)} headers given the include context their users supply"
              f" ({dropped_self} self-including entries pruned)")
    return made


# ---------------------------------------------------------------------------
# targets and flag probing
# ---------------------------------------------------------------------------

# clangd derives a target from the driver's name: aarch64-linux-gnu-gcc-16
# means --target=aarch64-linux-gnu; a bare gcc-16 leaves clang on its default.
# Mirror that here so probing sees the target clangd will actually use.
_TRIPLE_RE = re.compile(r"^([A-Za-z0-9_]+-[A-Za-z0-9_]+(?:-[A-Za-z0-9_.]+)*)-"
                        r"(?:gcc|clang|cc)(?:-[0-9.]+)?$")

_HOST_TRIPLE: str | None = None


def host_triple() -> str:
    global _HOST_TRIPLE
    if _HOST_TRIPLE is None:
        _HOST_TRIPLE = subprocess.run(["clang", "-dumpmachine"], capture_output=True,
                                      text=True, check=True).stdout.strip()
    return _HOST_TRIPLE


def driver_triple(driver: str) -> str:
    m = _TRIPLE_RE.match(os.path.basename(driver))
    return m.group(1) if m else host_triple()


# Sub-builds that compile for a different machine than the tree's own.  Keyed
# by source path, because that is what a config fragment can match on.
SUBTARGETS = [
    ("i386-linux-gnu", ("arch/x86/boot/", "arch/x86/realmode/")),
    ("riscv32-linux-gnu", ("arch/riscv/kernel/compat_vdso/", "arch/riscv/kernel/vdso32/")),
]

_SKIP_WITH_ARG = {"-o", "-c", "-include", "-isystem", "-I", "-D", "-U",
                  "-MT", "-MF", "-x", "-idirafter", "-iquote", "-imacros"}


def collect_flags(tree: str, entries: list[dict]) -> tuple[set[str], dict[str, set[str]]]:
    """Unique compile flags, split into the tree's own and each sub-build's."""
    ksrc = kernel_root(tree) + "/"
    main: set[str] = set()
    per_sub: dict[str, set[str]] = {}
    for e in entries:
        rel = e["file"][len(ksrc):] if e["file"].startswith(ksrc) else e["file"]
        bucket = main
        for _, prefixes in SUBTARGETS:
            for p in prefixes:
                if rel.startswith(p):
                    bucket = per_sub.setdefault(p, set())
                    break
        args = entry_args(e)
        i = 1
        while i < len(args):
            a = args[i]
            if a in _SKIP_WITH_ARG:
                i += 2
                continue
            if not a.startswith("-") or a.startswith(("-I", "-D", "-U", "-o", "-Wp,-M")):
                i += 1
                continue
            bucket.add(a)
            i += 1
    return main, per_sub


# A warning clang answers a flag with still reaches the editor as a diagnostic
# pinned to line 1 of every file in the tree, so both of these count as refused.
# Errors are matched on "error:" rather than on a list of phrasings: clang has
# many ways to say no -- "invalid arch name", "requires -menable-experimental-
# extensions", "unsupported option for target" -- and a list of them is a list
# that will be wrong for the next flag or the next clang.
_REJECT_WARNING = re.compile(
    r"unknown warning option|is not supported|not a recognized feature|"
    r"argument unused during compilation")
_REJECT_ERROR = re.compile(r"\berror:")


def probe_flags(flags: set[str], triple: str, jobs: int = 12) -> set[str]:
    """Return the flags this clang refuses for this target.

    Anything clang answers with an error is removed because it breaks the
    parse; anything it answers with a warning is removed too, because that
    warning reaches the editor as a diagnostic pinned to line 1 of every file
    in the tree.
    """
    if not flags:
        return set()
    fd, src = tempfile.mkstemp(suffix=".c", prefix="clangd_probe_")
    with os.fdopen(fd, "w") as f:
        f.write("int probe_translation_unit;\n")

    def one(flag: str) -> tuple[str, bool]:
        r = subprocess.run(["clang", f"--target={triple}", "-fsyntax-only", flag, src],
                           capture_output=True, text=True)
        bad = bool(_REJECT_ERROR.search(r.stderr)) or r.returncode != 0
        return flag, bad or bool(_REJECT_WARNING.search(r.stderr))

    bad = set()
    try:
        with cf.ThreadPoolExecutor(jobs) as ex:
            for flag, rejected in ex.map(one, sorted(flags)):
                if rejected:
                    bad.add(flag)
    finally:
        try:
            os.unlink(src)
        except OSError:
            pass
    return bad


def generalize(flags) -> list[str]:
    """Collapse -fmin-function-alignment=8 and =16 into one trailing-* glob, so
    a tree rebuilt with a different value does not need this regenerated."""
    by_key: dict[str, list[str]] = {}
    plain: list[str] = []
    for f in flags:
        key, sep, _ = f.partition("=")
        if sep:
            by_key.setdefault(key, []).append(f)
        else:
            plain.append(f)
    out = list(plain)
    for key, vals in by_key.items():
        out.append(f"{key}=*" if len(vals) > 1 else vals[0])
    return sorted(out)


# ---------------------------------------------------------------------------
# config fragments
# ---------------------------------------------------------------------------

# Warning classes that only ever fire because clangd is reading a GCC command
# line, never because of anything in the source.
DRIVER_QUIET = [
    "-Wno-unknown-warning-option",         # -Wno-dangling-pointer and friends
    "-Wno-ignored-optimization-argument",  # -fno-reorder-blocks and friends
    "-Qunused-arguments",
]

# "ignore every unused" on the compiler side.  -Wno-unused is a group, but the
# kernel enables several of these individually and a group flag does not undo a
# later specific one, so each is named.
UNUSED_QUIET = [
    "-Wno-unused",
    "-Wno-unused-variable",
    "-Wno-unused-function",
    "-Wno-unused-parameter",
    "-Wno-unused-but-set-variable",
    "-Wno-unused-but-set-parameter",
    "-Wno-unused-const-variable",
    "-Wno-unused-local-typedef",
    "-Wno-unused-label",
    "-Wno-unused-value",
    "-Wno-unused-result",
    "-Wno-unused-macros",
    "-Wno-unused-command-line-argument",
    "-Wno-unneeded-internal-declaration",
]

# Warnings clang emits on kernel source that GCC does not, so a GCC-built tree
# never carried a -Wno- for them.  Each is a difference of opinion between the
# two compilers about idiom, not a defect the build would ever act on.
CLANG_ONLY_NOISE = [
    "-Wno-asm-operand-widths",                    # arm64 %w0 vs %0 in inline asm
    "-Wno-gnu-variable-sized-type-not-at-end",    # trailing flex arrays in unions
    "-Wno-language-extension-token",              # __asm__ __volatile__ spellings
    "-Wno-c23-extensions",
    "-Wno-microsoft-anon-tag",
    "-Wno-fixed-enum-extension",
    "-Wno-gnu-folding-constant",
    "-Wno-null-pointer-arithmetic",
    "-Wno-pointer-to-int-cast",
    "-Wno-void-pointer-to-enum-cast",
    "-Wno-enum-enum-conversion",                  # BPF flag enums OR'd together
    "-Wno-enum-compare-conditional",
    "-Wno-format-invalid-specifier",              # %Zu, a pre-2018 kernel spelling
    "-Wno-format-extra-args",
    "-Wno-default-const-init-field-unsafe",       # clang 19+, kernel predates it
    "-Wno-default-const-init-var-unsafe",
    "-Wno-default-const-init-unsafe",
    "-Wno-unaligned-access",
    "-Wno-tautological-constant-out-of-range-compare",
    "-Wno-tautological-compare",
    # A designated initializer that overrides an earlier one is how the kernel
    # writes a table with a default: [0 ... N] = fallback, then the specific
    # entries.  GCC is told -Wno-override-init for it; clang spells the same
    # warning -Winitializer-overrides, and a tree old enough to predate the
    # clang build support never learned to pass it.
    "-Wno-initializer-overrides",
    # sizeof(a)/sizeof(a[0]) inside ARRAY_SIZE, on an array clang decides is a
    # pointer-sized thing.  A known false positive on kernel macros.
    "-Wno-sizeof-array-div",
    # Both of these the kernel turns off itself from v5.x on, so only a tree
    # old enough to predate its clang support still needs them named here.
    # Taking the address of a packed member is how the kernel walks every
    # on-the-wire and firmware structure it has; and an array declared [1] and
    # indexed further is its variable-length-array-at-the-end idiom, which
    # -Warray-bounds reads literally.
    "-Wno-address-of-packed-member",
    "-Wno-array-bounds",
    # `const const` out of a macro expansion, and the deliberate truncations
    # the kernel writes as ~0UL masks assigned into a u32.
    "-Wno-duplicate-decl-specifier",
    "-Wno-constant-conversion",
    # clang 18+.  Fires on out-parameters passed as const pointers, which the
    # kernel does constantly; the callee fills them in.
    "-Wno-uninitialized-const-pointer",
    # sprintf(str + strlen(str), ...) is how the kernel's own build tools
    # assemble a string; clang reads the pointer arithmetic as an attempt to
    # append to a literal.
    "-Wno-string-plus-int",
    # An enum constant used as a condition, which is how the tracing macros
    # are written.  clang's spelling of this has no GCC counterpart to match.
    "-Wno-int-in-bool-context",
    # `if (p->name)` where name is an array member: always true, and the
    # kernel writes it deliberately to mean "the containing object exists".
    "-Wno-pointer-bool-conversion",
    # __section() spellings clang and GCC disagree about attaching to a
    # forward declaration.
    "-Wno-section",
]

# Compiler-provided intrinsic headers are not the same header in clang and GCC:
# clang's arm_neon.h types uint64_t as unsigned long long where GCC's uses
# unsigned long, so every pointer assignment in a NEON routine reads as a type
# error -- one clang produces about its own header, in a tree GCC compiled
# cleanly.  Files that reach for these headers get the narrow suppressions that
# difference needs.
INTRINSIC_HEADERS = ("arm_neon.h", "arm_sve.h", "arm_acle.h", "arm_bf16.h",
                     "immintrin.h", "x86intrin.h", "emmintrin.h", "xmmintrin.h",
                     "riscv_vector.h", "riscv_crypto.h", "altivec.h")

INTRINSIC_QUIET = [
    "-Wno-incompatible-pointer-types",
    "-Wno-int-conversion",
    "-Wno-incompatible-function-pointer-types",
]

# Makefiles that carry the tree's own opinion about which warnings to silence,
# including the ones it only silences when built by clang.  Reading them means
# an older or newer tree contributes its own list instead of this script's.
WARNING_MAKEFILES = (
    "Makefile",
    "scripts/Makefile.extrawarn",
    "scripts/Makefile.lib",
)

_WNO_RE = re.compile(r"-Wno-[a-z0-9-]+")


def kernel_warning_opinions(tree: str, triple: str) -> list[str]:
    """Every -Wno- the makefiles mention, kept only where this clang knows it.

    Read from every tree in the workspace, not just this one.  A kernel from
    2016 predates the kernel's clang support and so has no way to say which
    clang warnings it considers noise on its own source -- but a 6.12 or
    mainline tree sitting next to it says exactly that, in
    scripts/Makefile.extrawarn, about code that has barely changed.  Applying
    the newer tree's judgement to the older one is the whole point: it is the
    same project's opinion about the same idioms, expressed where it could be.

    A GCC-only spelling is dropped here rather than reaching the editor as an
    unknown-warning-option diagnostic on line 1 of every file.
    """
    found: set[str] = set()
    for name in discover_trees():
        ksrc = kernel_root(name)
        paths = list(WARNING_MAKEFILES)
        arch_dir = os.path.join(ksrc, "arch")
        if os.path.isdir(arch_dir):
            paths += [os.path.join("arch", a, "Makefile")
                      for a in os.listdir(arch_dir)]
        for rel in paths:
            p = os.path.join(ksrc, rel)
            if not os.path.isfile(p):
                continue
            try:
                with open(p, encoding="utf-8", errors="replace") as f:
                    found.update(_WNO_RE.findall(f.read()))
            except OSError:
                continue
    found.discard("-Wno-unknown-warning-option")
    return sorted(found - set(probe_flags(found, triple)))

# clang-tidy checks that fire on kernel idiom rather than on kernel mistakes.
# sizeof() on a pointer expression is how the fixed-size copy helpers are
# written; the indentation checks read a macro body's formatting as the
# caller's; include-cleaner does not understand a tree where every header
# expects an ordering.  Everything not listed here stays on -- that is the
# point of removing checks one at a time instead of switching clang-tidy off.
TIDY_REMOVE = [
    "bugprone-sizeof-expression",
    "bugprone-macro-parentheses",
    "bugprone-branch-clone",
    "bugprone-assignment-in-if-condition",
    "bugprone-narrowing-conversions",
    "bugprone-implicit-widening-of-multiplication-result",
    "bugprone-easily-swappable-parameters",
    "bugprone-reserved-identifier",
    "bugprone-signed-char-misuse",
    "bugprone-suspicious-include",
    "bugprone-too-small-loop-variable",
    "bugprone-misplaced-widening-cast",
    "bugprone-integer-division",
    "bugprone-not-null-terminated-result",
    "bugprone-multi-level-implicit-pointer-conversion",
    "bugprone-casting-through-void",
    "bugprone-switch-missing-default-case",
    "bugprone-unused-return-value",
    "bugprone-unused-local-non-trivial-variable",
    "readability-misleading-indentation",
    "readability-redundant-declaration",
    "readability-non-const-parameter",
    "readability-inconsistent-declaration-parameter-name",
    "readability-duplicate-include",
    "readability-math-missing-parentheses",
    "misc-unused-parameters",
    "misc-unused-alias-decls",
    "misc-redundant-expression",
    "misc-no-recursion",
    "misc-include-cleaner",
    "clang-analyzer-*",
]

# misc-header-include-cycle and misc-const-correctness belong on that list by
# behaviour -- both fire constantly on kernel source -- but naming them there
# makes clangd warn on its own config file: it classifies them as slow checks,
# which FastCheckFilter already stops from ever running.  Removing what is
# already off is what the warning objects to, so the filter is stated instead
# and the two names are left out.  Set FastCheckFilter to None below and they
# would come back; that is the setting to change, not this list.
TIDY_FAST_FILTER = "Strict"


def indent(items, pad: str) -> str:
    return "".join(f"{pad}- {i}\n" for i in items)


def path_regex(*parts: str) -> str:
    """A PathMatch anchored at an absolute lab path.  User config matches
    absolute paths, so the tree prefix is what keeps these fragments from
    reaching any other project on this machine."""
    return "".join(parts)


def coverage(tree: str, entries: list[dict]) -> tuple[set[str], set[str]]:
    """Which top-level directories, and which arch/<name>, this tree actually
    compiles.  A tree configured for arm64 never builds arch/powerpc, so every
    diagnostic clangd could produce there is an artefact of reading powerpc
    source through an aarch64 command line -- there is no correct parse to
    reach.  Same for tools/ and samples/, which are separate builds."""
    ksrc = kernel_root(tree) + "/"
    tops: set[str] = set()
    arches: set[str] = set()
    for e in entries:
        rel = e["file"][len(ksrc):] if e["file"].startswith(ksrc) else e["file"]
        head, _, _ = rel.partition("/")
        tops.add(head)
        if head == "arch":
            arches.add(rel.split("/")[1])
    return tops, arches


def srcarch(tree: str) -> str | None:
    """The one arch directory this tree is configured for -- kbuild's SRCARCH,
    which is not always the tree.conf ARCH: x86_64 builds arch/x86."""
    conf_arch = TREES[tree]["arch"]
    return {"x86_64": "x86", "i386": "x86", "riscv64": "riscv",
            "riscv32": "riscv", "arm64": "arm64"}.get(conf_arch, conf_arch)


def tree_fragments(tree: str, entries: list[dict], verbose: bool,
                   header_diagnostics: bool = False) -> list[str]:
    ksrc = kernel_root(tree)
    prefix = re.escape(ksrc) + "/"
    driver = entry_args(entries[0])[0]
    triple = driver_triple(driver)
    main_flags, per_sub = collect_flags(tree, entries)

    # Probe the sub-builds' flags against the main target too.  A header has no
    # database entry of its own, so clangd borrows the command of whatever file
    # is nearest -- and for arch/sparc/include/asm/*.h in an x86_64 tree, that
    # is the i386 boot stub.  -mpreferred-stack-boundary=2 then reaches a file
    # the sub-build fragment's PathMatch will never cover, and shows up as a
    # driver error on line 1.  Removing it tree-wide costs nothing: the
    # sub-build fragment names the real target, which is what the parse needs.
    all_flags = set(main_flags)
    for flags in per_sub.values():
        all_flags |= flags
    bad = probe_flags(all_flags, triple)
    if verbose:
        print(f"    target {triple}: {len(bad)}/{len(all_flags)} flags rejected by clang")

    # The build's toolchain lives in a container, so its -isystem paths do not
    # exist on this host.  A pre-5.x kernel includes <stdarg.h> from
    # linux/kernel.h and needs some freestanding header directory to be real;
    # clang's own serves.  -isystem takes a separate argument, so the whole
    # option is dropped -- removing just the path would leave -isystem to
    # swallow the next -I -- and the reachable ones are added back.
    isystem = set()
    for e in entries:
        args = entry_args(e)
        for i, a in enumerate(args):
            if a == "-isystem" and i + 1 < len(args):
                isystem.add(args[i + 1])
            elif a.startswith("-isystem") and len(a) > len("-isystem"):
                isystem.add(a[len("-isystem"):])
    missing_isystem = {p for p in isystem if not os.path.isdir(p)}

    remove = generalize(bad)
    add = list(DRIVER_QUIET + UNUSED_QUIET) + CLANG_ONLY_NOISE
    own_opinions = kernel_warning_opinions(tree, triple)
    add += [w for w in own_opinions if w not in add]
    if verbose:
        print(f"    {len(own_opinions)} warning suppressions taken from the tree's makefiles")
    if missing_isystem:
        res = subprocess.run(["clang", "-print-resource-dir"], capture_output=True,
                             text=True, check=True).stdout.strip()
        remove.append("-isystem")
        add += [f"-isystem{p}" for p in sorted(isystem - missing_isystem)]
        add.append(f"-isystem{os.path.join(res, 'include')}")
        if verbose:
            print(f"    {len(missing_isystem)} unreachable -isystem path(s) "
                  f"replaced with clang's own headers"
                  + (f", {len(isystem - missing_isystem)} kept"
                     if isystem - missing_isystem else ""))

    frags = []
    head = [
        f"# {tree}: {os.path.basename(driver)} -> {triple}",
        "If:",
        f"  PathMatch: {path_regex(prefix, '.*')}",
        "CompileFlags:",
        f"  CompilationDatabase: {cache_dir(tree)}",
    ]
    if remove:
        head += ["  Remove:", indent(remove, "    ").rstrip("\n")]
    head += ["  Add:", indent(add, "    ").rstrip("\n")]
    head += [
        "Diagnostics:",
        # clangd's own unused signals are not compiler warnings and survive
        # every -Wno- above: the greyed-out #include, and the header-not-used
        # hint.  Kernel headers pull each other in constantly; both are noise.
        "  UnusedIncludes: None",
        "  MissingIncludes: None",
        "  ClangTidy:",
        f"    FastCheckFilter: {TIDY_FAST_FILTER}",
        "    Remove:",
        indent(TIDY_REMOVE, "      ").rstrip("\n"),
    ]
    frags.append("\n".join(head) + "\n")

    for sub_triple, prefixes in SUBTARGETS:
        hit = [p for p in prefixes if per_sub.get(p)]
        if not hit:
            continue
        flags: set[str] = set()
        for p in hit:
            flags |= per_sub[p]
        sub_bad = probe_flags(flags, sub_triple)
        if verbose:
            print(f"    sub-build {sub_triple} {hit}: "
                  f"{len(sub_bad)}/{len(flags)} flags rejected")
        pat = "|".join(re.escape(p) for p in hit)
        sub = [
            f"# {tree}: built for {sub_triple}, not for the tree's own target",
            "If:",
            f"  PathMatch: {path_regex(prefix, '(', pat, ').*')}",
            "CompileFlags:",
            "  Add:",
            f"    - --target={sub_triple}",
            indent(DRIVER_QUIET + UNUSED_QUIET, "    ").rstrip("\n"),
        ]
        if sub_bad:
            sub += ["  Remove:", indent(generalize(sub_bad), "    ").rstrip("\n")]
        frags.append("\n".join(sub) + "\n")

    # Files that include a compiler-provided intrinsic header.  Found by
    # reading the sources rather than by keeping a list, because which drivers
    # reach for NEON or AVX changes between versions.
    # The kernel does not reach for arm_neon.h directly; arch/arm64/lib/xor-neon.c
    # includes asm/neon-intrinsics.h, which includes it.  So the wrappers are
    # found first, then the files that include a wrapper.
    inc_re = re.compile(r'^\s*#\s*include\s*[<"](' +
                        "|".join(re.escape(h) for h in INTRINSIC_HEADERS) + r')[>"]',
                        re.M)

    def uses_intrinsics(path: str) -> bool:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                return bool(inc_re.search(fh.read(65536)))
        except OSError:
            return False

    wrappers: set[str] = set()
    for root, dirs, files in os.walk(os.path.join(ksrc, "arch")):
        dirs[:] = [d for d in dirs if d not in (".git",)]
        for name in files:
            if name.endswith(".h") and uses_intrinsics(os.path.join(root, name)):
                wrappers.add(os.path.relpath(os.path.join(root, name), ksrc))
    wrapper_re = re.compile(
        r'^\s*#\s*include\s*[<"]([^>"]*(?:'
        + "|".join(re.escape(os.path.basename(w)) for w in sorted(wrappers))
        + r'))[>"]', re.M) if wrappers else None

    intrinsic_files: list[str] = sorted(wrappers)
    for e in entries:
        f = e["file"]
        rel = os.path.relpath(f, ksrc)
        if uses_intrinsics(f):
            intrinsic_files.append(rel)
            continue
        if wrapper_re:
            try:
                with open(f, encoding="utf-8", errors="replace") as fh:
                    if wrapper_re.search(fh.read(65536)):
                        intrinsic_files.append(rel)
            except OSError:
                continue
    intrinsic_files = sorted(set(intrinsic_files))
    if intrinsic_files:
        if verbose:
            print(f"    {len(intrinsic_files)} file(s) use compiler intrinsic headers: "
                  f"{', '.join(intrinsic_files[:3])}"
                  + (" ..." if len(intrinsic_files) > 3 else ""))
        pat = "|".join(re.escape(p) for p in sorted(intrinsic_files))
        frags.append("\n".join([
            f"# {tree}: clang's intrinsic headers type these differently than GCC's",
            "If:",
            f"  PathMatch: {path_regex(prefix, '(', pat, ')')}",
            "CompileFlags:",
            "  Add:",
            indent(INTRINSIC_QUIET, "    ").rstrip("\n"),
        ]) + "\n")

    # Source this tree never compiles.  An arch it is not configured for has no
    # correct parse to reach through this command line, and neither has tools/
    # or samples/, which are built by something else entirely.  Silencing them
    # costs nothing real: navigation, hover and completion still work, and the
    # diagnostics that go away could only ever have been wrong.
    tops, built_arches = coverage(tree, entries)
    own = srcarch(tree)
    arch_dir = os.path.join(ksrc, "arch")
    dead: list[str] = []
    if os.path.isdir(arch_dir):
        for a in sorted(os.listdir(arch_dir)):
            if not os.path.isdir(os.path.join(arch_dir, a)) or a == own:
                continue
            # An arch with sources in the database (arm under arm64, for its
            # shared xen code) still has headers written for a machine this
            # tree is not; only those are silenced.
            dead.append(f"arch/{a}/include/" if a in built_arches else f"arch/{a}/")
    for top in sorted(os.listdir(ksrc)):
        if not os.path.isdir(os.path.join(ksrc, top)) or top.startswith("."):
            continue
        if top in tops or top in ("arch", "include"):
            continue
        dead.append(f"{top}/")
    if dead:
        if verbose:
            not_built = [d for d in dead if not d.startswith("arch/")]
            print(f"    {len(dead)} paths this tree never compiles are silenced "
                  f"({len(dead) - len(not_built)} foreign arch, "
                  f"{len(not_built)} non-kernel: {', '.join(sorted(not_built)[:6])})")
        pat = "|".join(re.escape(d) for d in dead)
        frags.append("\n".join([
            f"# {tree}: not built here -- no command line makes this parse correctly",
            "If:",
            f"  PathMatch: {path_regex(prefix, '(', pat, ').*')}",
            "CompileFlags:",
            # Suppress silences named diagnostics, but clangd reports the
            # "too many errors" fatal outside that filter -- it is how the AST
            # build failed, not a diagnostic about the code.  Lifting the limit
            # removes the fatal at its source.  It is affordable here precisely
            # because these files are never going to parse: the parse stops on
            # structure, not on a hundred separate complaints.
            "  Add:",
            "    - -ferror-limit=0",
            "Diagnostics:",
            "  Suppress: '*'",
        ]) + "\n")

    # Kernel headers are written to be included in an order, not opened alone:
    # asm/atomic_lse.h has no idea what atomic_t is, because in a real build
    # asm/atomic.h got there first.  linux/kernel.h in front supplies most of
    # that context, which is what makes hover and go-to-definition work inside
    # a header at all -- but "most" is not "all", and what is left over is a
    # diagnostic about the reading, never about the header.  So the context is
    # forced and the diagnostics are dropped.  The error limit stays finite on
    # purpose: on a header that does fall apart, an unlimited one costs minutes
    # of parse time for errors nobody will see.
    hdr = [
        f"# {tree}: headers get build context; what still fails is the reading, not the header",
        "If:",
        f"  PathMatch: {path_regex(prefix, r'.*\.(h|hpp|hh|hxx|inc)')}",
        "CompileFlags:",
        "  Add:",
        "    - -include",
        "    - linux/kernel.h",
        "    - -ferror-limit=100",
        "    - -Wno-macro-redefined",
        "    - -Wno-builtin-macro-redefined",
    ]
    if not header_diagnostics:
        hdr += ["    - -ferror-limit=0", "Diagnostics:", "  Suppress: '*'"]
    frags.append("\n".join(hdr) + "\n")
    return frags


def read_user_config() -> str:
    if os.path.isfile(USER_CONFIG):
        with open(USER_CONFIG) as f:
            return f.read()
    return ""


def write_tree_block(tree: str, blocks: list[str] | None) -> None:
    """Replace one tree's marked block, leaving every other tree -- and
    anything the operator wrote themselves -- untouched.

    Per-tree markers are what makes `kbuildlab tags v6.12-arm64` safe to run:
    the other trees' fragments are not regenerated, so a tree whose database
    has not been rebuilt keeps the configuration that matches it.  Passing
    None removes the block.
    """
    begin, end = block_markers(tree)
    existing = read_user_config()
    body = ""
    if blocks:
        body = begin + "\n" + "\n---\n".join(blocks) + end + "\n"
    if begin in existing and end in existing:
        pre = existing.split(begin)[0]
        post = existing.split(end, 1)[1]
        new = pre + body + post
    elif body:
        new = (existing.rstrip("\n") + "\n---\n" + body) if existing.strip() else body
    else:
        return
    new = re.sub(r"\n---\s*\n(\s*---\s*\n)+", "\n---\n", new)
    new = re.sub(r"\A(\s*---\s*\n)+", "", new)
    os.makedirs(os.path.dirname(USER_CONFIG), exist_ok=True)
    if new.strip():
        with open(USER_CONFIG, "w") as f:
            f.write(new)
    elif os.path.isfile(USER_CONFIG):
        os.unlink(USER_CONFIG)


# ---------------------------------------------------------------------------
# LSP verification
# ---------------------------------------------------------------------------

SEVERITY = {1: "error", 2: "warning", 3: "info", 4: "hint"}

# One clangd parsing kernel sources holds on to roughly a gigabyte, so how many
# can run at once is a memory question and not a core-count one.  Fixing the
# number is what makes a 16 GiB machine swap: the right count depends on what
# else is running, which is not knowable when the flag is typed.
SESSION_MB = 1200          # measured RSS of a clangd chewing through kernel code
# Enough for an editor with its own clangd (or several), a browser, and the
# work the operator actually sat down to do.  Measured the hard way: at 3 GiB
# reserved, a scan and an editor indexing the same trees drove a 16 GiB machine
# to the point where the kernel started killing things.
RESERVE_MB = 3584


def available_mb() -> int | None:
    """MemAvailable, which already accounts for reclaimable cache -- unlike
    MemFree, which on a machine that has just built a kernel reads as nearly
    zero and says nothing about what can actually be allocated."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except OSError:
        pass
    return None


def auto_jobs(cap: int | None = None, reserve_mb: int = RESERVE_MB) -> int:
    cpus = os.cpu_count() or 2
    ceiling = cap or max(1, cpus - 1)
    av = available_mb()
    if av is None:
        return min(2, ceiling)
    return max(1, min(ceiling, (av - reserve_mb) // SESSION_MB))


def memory_is_tight(reserve_mb: int = RESERVE_MB) -> bool:
    av = available_mb()
    return av is not None and av < reserve_mb


class Session:
    """One clangd process, driven over stdio the way an editor drives it."""

    def __init__(self, root: str):
        self.root = root
        self.proc = subprocess.Popen(
            ["clangd", "--background-index=false", "--clang-tidy", "--log=error", "-j=1"],
            cwd=root, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL)
        self.next_id = 0
        self.replies: dict[int, dict] = {}
        self.diags: dict[str, list] = {}
        self.lock = threading.Lock()
        self.arrived = threading.Event()
        threading.Thread(target=self._read_loop, daemon=True).start()
        self._request("initialize", {
            "processId": os.getpid(),
            "rootUri": "file://" + root,
            "capabilities": {"textDocument": {"publishDiagnostics": {}}},
        })
        self._notify("initialized", {})

    def _write(self, obj: dict) -> None:
        body = json.dumps(obj).encode()
        assert self.proc.stdin is not None
        self.proc.stdin.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
        self.proc.stdin.flush()

    def _notify(self, method: str, params) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def _request(self, method: str, params, wait: float = 120.0):
        self.next_id += 1
        rid = self.next_id
        self._write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        deadline = time.time() + wait
        while time.time() < deadline:
            with self.lock:
                if rid in self.replies:
                    return self.replies.pop(rid)
            time.sleep(0.01)
        return None

    def _read_loop(self) -> None:
        out = self.proc.stdout
        assert out is not None
        while True:
            line = out.readline()
            if not line:
                return
            if not line.startswith(b"Content-Length:"):
                continue
            n = int(line.split(b":")[1])
            out.readline()
            msg = json.loads(out.read(n))
            with self.lock:
                if "id" in msg and "method" not in msg:
                    self.replies[msg["id"]] = msg
                elif msg.get("method") == "textDocument/publishDiagnostics":
                    p = msg["params"]
                    self.diags[p["uri"]] = p["diagnostics"]
                    self.arrived.set()
                elif "id" in msg:
                    self._write({"jsonrpc": "2.0", "id": msg["id"], "result": None})

    def diagnose(self, path: str, timeout: float = 60.0) -> list:
        uri = "file://" + path
        with self.lock:
            self.diags.pop(uri, None)
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError as exc:
            return [{"severity": 1, "code": "unreadable", "message": str(exc),
                     "range": {"start": {"line": 0}}}]
        self.arrived.clear()
        self._notify("textDocument/didOpen", {"textDocument": {
            "uri": uri, "languageId": "c", "version": 1, "text": text}})
        deadline = time.time() + timeout
        result = None
        while time.time() < deadline:
            with self.lock:
                if uri in self.diags:
                    result = self.diags[uri]
                    break
            self.arrived.wait(0.2)
            self.arrived.clear()
        if result is None:
            # clangd publishes nothing at all for some files that have nothing
            # to say -- a dt-bindings header of bare #defines, for one.  Nudge
            # it with an edit before calling that a finding, so a silent file
            # is not counted as a broken one.
            self._notify("textDocument/didChange", {
                "textDocument": {"uri": uri, "version": 2},
                "contentChanges": [{"text": text + "\n"}]})
            deadline = time.time() + 20
            while time.time() < deadline:
                with self.lock:
                    if uri in self.diags:
                        result = self.diags[uri]
                        break
                self.arrived.wait(0.2)
                self.arrived.clear()
        self._notify("textDocument/didClose", {"textDocument": {"uri": uri}})
        if result is None:
            # Still nothing.  Ask clangd the same question the other way --
            # --check parses the file and prints what it found -- so a file
            # that simply has nothing to report is not recorded as a failure.
            try:
                r = subprocess.run(
                    ["clangd", f"--check={path}", "--check-locations=0", "--log=error"],
                    cwd=self.root, capture_output=True, text=True, timeout=120)
                if not re.search(r"^E\[", r.stderr, re.M):
                    return []
                first = next((l for l in r.stderr.splitlines() if l.startswith("E[")), "")
                return [{"severity": 1, "code": "check-only",
                         "message": first[:200] or "reported by --check",
                         "range": {"start": {"line": 0}}}]
            except Exception:
                pass
            return [{"severity": 1, "code": "timeout",
                     "message": f"no diagnostics within {timeout:.0f}s",
                     "range": {"start": {"line": 0}}}]
        return result

    def close(self) -> None:
        try:
            self._request("shutdown", None, wait=5)
            self._notify("exit", None)
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()


# Where a kernel breaks a C parser first: arch code, the atomics, the vDSOs,
# the boot stubs, and the headers nobody ever compiles on their own.
_SOURCE_CORNERS = (
    r"arch/[^/]+/kernel/", r"arch/[^/]+/mm/", r"arch/[^/]+/boot/",
    r"arch/[^/]+/entry/", r"arch/[^/]+/lib/", r"arch/[^/]+/kvm/",
    r"arch/[^/]+/crypto/", r"vdso", r"realmode", r"purgatory",
    r"^mm/", r"^kernel/sched/", r"^kernel/locking/", r"^kernel/bpf/",
    r"^kernel/trace/", r"^kernel/rcu/", r"^lib/", r"^fs/", r"^net/",
    r"^crypto/", r"^drivers/", r"^init/", r"^block/", r"^ipc/",
    r"^security/", r"^virt/", r"^sound/",
)

_HEADER_DIRS = (
    "include/linux", "include/asm-generic", "include/uapi/linux", "include/net",
    "include/crypto", "include/trace", "include/vdso", "include/acpi",
    "include/drm", "include/media", "include/sound", "include/scsi",
)


def all_files(tree: str) -> list[str]:
    """Every file the database names, headers included."""
    return sorted({e["file"] for e in cdb_entries(tree)})


def sample_files(tree: str, n: int, seed: int) -> list[str]:
    """A deterministic spread over sources, headers and the awkward corners."""
    ksrc = kernel_root(tree)
    sources = sorted(e["file"] for e in cdb_entries(tree))
    rng = random.Random(seed)
    picked = set(rng.sample(sources, min(n, len(sources))))

    per_corner = max(1, n // 30)
    for pat in _SOURCE_CORNERS:
        rx = re.compile(pat)
        hits = [f for f in sources if rx.search(f[len(ksrc) + 1:])]
        picked.update(rng.sample(hits, min(per_corner, len(hits))) if hits else [])

    arch_dirs = [d for d in sorted(os.listdir(os.path.join(ksrc, "arch")))
                 if os.path.isdir(os.path.join(ksrc, "arch", d, "include", "asm"))]
    header_dirs = list(_HEADER_DIRS) + [f"arch/{a}/include/asm" for a in arch_dirs]
    per_dir = max(2, n // 10)
    for rel in header_dirs:
        d = os.path.join(ksrc, rel)
        if not os.path.isdir(d):
            continue
        names = sorted(x for x in os.listdir(d) if x.endswith(".h"))
        picked.update(os.path.join(d, x) for x in names[:per_dir])
    return sorted(picked)


def run_check(tree: str, files: list[str], jobs: int, journal=None,
              progress_every: int = 0):
    """Diagnose every file, optionally journalling as it goes.

    An exhaustive pass over a kernel tree is tens of thousands of files and
    takes hours, so each result is written the moment it lands: the run can be
    interrupted and resumed rather than started over.  Workers are also
    recycled -- a clangd session that has parsed a few hundred kernel files
    holds on to enough memory that a long run would otherwise crowd out the
    editor's own clangd.
    """
    root = kernel_root(tree)
    work: queue.Queue = queue.Queue()
    for f in files:
        work.put(f)
    results: list[tuple[str, list]] = []
    rlock = threading.Lock()
    done = [0]
    live = [jobs]
    RECYCLE = 250

    def worker():
        session = Session(root)
        served = 0
        try:
            while True:
                try:
                    f = work.get_nowait()
                except queue.Empty:
                    return
                # Under pressure, retire this worker instead of pausing it.
                #
                # Pausing was the first attempt and it oscillates: the sessions
                # are themselves most of the pressure, so they all find memory
                # tight, all stop, all see it free, and all start again --
                # paying for a fresh preamble every cycle and making no
                # progress.  Retiring converges instead.  One worker leaves,
                # its arena goes back, and the rest carry on; if it is still
                # tight the next one leaves too, until what is left fits.  The
                # queue is shared, so nothing is dropped -- the same files get
                # scanned by fewer sessions.
                if memory_is_tight():
                    with rlock:
                        if live[0] > 1:
                            live[0] -= 1
                            n_left = live[0]
                            work.put(f)          # hand the file back
                            retire = True
                        else:
                            retire = False
                    if retire:
                        session.close()
                        print(f"      memory is tight -- one scan session "
                              f"retired, {n_left} left", flush=True)
                        return
                    # The last one stays: something has to make progress, and
                    # a single session is a smaller footprint than the editor.
                d = session.diagnose(f)
                with rlock:
                    done[0] += 1
                    if d:
                        results.append((f, d))
                    if journal is not None:
                        journal.write(json.dumps(
                            {"file": os.path.relpath(f, root),
                             "diags": [{"sev": x.get("severity", 1),
                                        "code": str(x.get("code", "")),
                                        "line": x["range"]["start"]["line"] + 1,
                                        "msg": x["message"].splitlines()[0][:300]}
                                       for x in d]}) + "\n")
                        journal.flush()
                    if progress_every and done[0] % progress_every == 0:
                        print(f"      {done[0]}/{len(files)} scanned, "
                              f"{len(results)} with diagnostics", flush=True)
                served += 1
                if served >= RECYCLE:
                    session.close()
                    session = Session(root)
                    served = 0
        finally:
            session.close()

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(jobs)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return sorted(results)


# ---------------------------------------------------------------------------
# warming the index
# ---------------------------------------------------------------------------

def warm_index(tree: str, jobs: int, timeout: float, quiet: bool = False) -> bool:
    """Build clangd's background index now, from the command line.

    Without this the index is built by whichever clangd the editor starts, in
    the background, while you are trying to read code -- and after a rebuild
    there is always something new to do, so it happens again.  It is the same
    work either way; doing it here means it is finished before an editor is
    opened, and finished on this machine's terms rather than in competition
    with the thing you actually wanted to do.

    It is incremental by construction: clangd keys its index shards on each
    file's contents, so a rebuild that changed forty files re-indexes forty
    files.  A tree already warm returns in seconds.

    Driven through the LSP progress notifications rather than by watching the
    cache directory: clangd says when it starts and when it has nothing left,
    and guessing from file timestamps would either stop early or never stop.
    """
    root = kernel_root(tree)
    proc = subprocess.Popen(
        ["clangd", "--background-index", f"-j={jobs}", "--log=error",
         "--background-index-priority=normal"],
        cwd=root, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL)
    state = {"begun": False, "done": False, "last": "", "replies": {}}
    lock = threading.Lock()

    def write(obj):
        body = json.dumps(obj).encode()
        proc.stdin.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
        proc.stdin.flush()

    def reader():
        out = proc.stdout
        while True:
            line = out.readline()
            if not line:
                return
            if not line.startswith(b"Content-Length:"):
                continue
            n = int(line.split(b":")[1])
            out.readline()
            try:
                msg = json.loads(out.read(n))
            except Exception:
                continue
            with lock:
                if msg.get("method") == "$/progress":
                    v = msg.get("params", {}).get("value", {})
                    kind = v.get("kind")
                    if kind == "begin":
                        state["begun"] = True
                    elif kind == "report":
                        state["begun"] = True
                        pct = v.get("percentage")
                        msgtxt = v.get("message", "")
                        state["last"] = f"{pct}% {msgtxt}".strip() if pct is not None else msgtxt
                    elif kind == "end":
                        state["done"] = True
                elif "id" in msg and "method" in msg:
                    write({"jsonrpc": "2.0", "id": msg["id"], "result": None})
                elif "id" in msg:
                    state["replies"][msg["id"]] = msg

    threading.Thread(target=reader, daemon=True).start()
    write({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "processId": os.getpid(),
        "rootUri": "file://" + root,
        "capabilities": {"window": {"workDoneProgress": True}},
    }})
    write({"jsonrpc": "2.0", "method": "initialized", "params": {}})

    # Indexing starts when clangd is given something to look at.  Any source
    # from the database will do; the first one keeps it deterministic.
    seed = next((e["file"] for e in cdb_entries(tree) if not is_header(e["file"])), None)
    if seed and os.path.isfile(seed):
        try:
            with open(seed, encoding="utf-8", errors="replace") as f:
                text = f.read()
            write({"jsonrpc": "2.0", "method": "textDocument/didOpen", "params": {
                "textDocument": {"uri": "file://" + seed, "languageId": "c",
                                 "version": 1, "text": text}}})
        except OSError:
            pass

    started = time.time()
    grace = 45.0            # how long to wait for indexing to even begin
    last_shown = 0.0
    ok = False
    try:
        while time.time() - started < timeout:
            with lock:
                begun, done, last = state["begun"], state["done"], state["last"]
            if done:
                ok = True
                break
            if not begun and time.time() - started > grace:
                ok = True       # nothing to do: the tree is already warm
                break
            if not quiet and last and time.time() - last_shown > 15:
                last_shown = time.time()
                print(f"      indexing {tree}: {last}", flush=True)
            time.sleep(0.5)
    finally:
        try:
            write({"jsonrpc": "2.0", "id": 2, "method": "shutdown", "params": None})
            write({"jsonrpc": "2.0", "method": "exit", "params": None})
            proc.wait(timeout=30)
        except Exception:
            proc.kill()
    return ok


def index_size(tree: str) -> str:
    d = os.path.join(cache_dir(tree), ".cache", "clangd", "index")
    if not os.path.isdir(d):
        return "none"
    total = 0
    files = 0
    for root, _, names in os.walk(d):
        for n in names:
            try:
                total += os.path.getsize(os.path.join(root, n))
                files += 1
            except OSError:
                pass
    return f"{files} shards, {total / (1 << 20):.0f} MiB"


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

def cmd_gen(args) -> int:
    for tree in args.trees:
        print(f"[{tree}]")
        kept, dropped = gen_cdb(tree, args.force)
        print(f"    compile_commands.json: {kept} entries"
              + (f" ({dropped} assembly entries dropped)" if dropped else "")
              + f"  -> {cache_dir(tree)}")
        if not args.no_overlay:
            arch = srcarch(tree) or "x86"
            kbuildinfo.build_overlay(kernel_root(tree), overlay_dir(tree), arch,
                                     verbose=True)
            n = apply_overlay(tree)
            if n:
                print(f"    overlay put ahead of the tree on {n} command lines")
        if not args.no_header_context:
            # Only the build's own entries are input here.  Re-running gen
            # without -f keeps the cached database, and feeding last run's
            # header entries back in would add a second copy of each -- which
            # is not merely wasteful: a compilation database with the same file
            # listed twice leaves clangd free to pick either, so the command a
            # header gets stops being predictable.
            src = [e for e in cdb_entries(tree) if not is_header(e["file"])]
            extra = header_entries(tree, src, verbose=True)
            cdb = os.path.join(cache_dir(tree), "compile_commands.json")
            with open(cdb, "w") as f:
                json.dump(src + extra, f, indent=1)
        blocks = tree_fragments(tree, cdb_entries(tree), verbose=True,
                                header_diagnostics=args.header_diagnostics)
        write_tree_block(tree, blocks)
        print(f"    {len(blocks)} config fragments -> {USER_CONFIG}")
        print(f"    nothing was written inside {kernel_root(tree)}")
    return 0


def cmd_check(args) -> int:
    grand_files = grand_diags = grand_errors = 0
    by_code: dict[str, int] = {}
    for tree in args.trees:
        if args.all:
            files = all_files(tree)
        else:
            files = sample_files(tree, args.number, args.seed)
        journal = None
        if args.journal:
            path = f"{args.journal}.{tree}.jsonl"
            seen: set[str] = set()
            if args.resume and os.path.isfile(path):
                with open(path) as jf:
                    for line in jf:
                        try:
                            seen.add(json.loads(line)["file"])
                        except Exception:
                            continue
                root = kernel_root(tree)
                before = len(files)
                files = [f for f in files if os.path.relpath(f, root) not in seen]
                print(f"[{tree}] resuming: {before - len(files)} already scanned, "
                      f"{len(files)} left")
            journal = open(path, "a" if args.resume else "w")
        jobs = args.jobs or auto_jobs()
        if not args.jobs:
            av = available_mb()
            print(f"[{tree}] {jobs} parallel session(s)"
                  + (f" ({av} MiB available, {RESERVE_MB} MiB reserved)" if av else ""))
        started = time.time()
        try:
            results = run_check(tree, files, jobs, journal,
                                progress_every=500 if args.all else 0)
        finally:
            if journal is not None:
                journal.close()
        took = time.time() - started
        ndiag = sum(len(d) for _, d in results)
        nerr = sum(1 for _, d in results for x in d if x.get("severity", 1) == 1)
        grand_files += len(files)
        grand_diags += ndiag
        grand_errors += nerr
        print(f"[{tree}] {len(files)} files | {len(results)} with diagnostics | "
              f"{ndiag} diagnostics, {nerr} errors | {took:.0f}s")
        for f, d in results:
            rel = os.path.relpath(f, kernel_root(tree))
            if args.verbose:
                print(f"  ### {rel}")
            for x in d:
                code = str(x.get("code", ""))
                by_code[code] = by_code.get(code, 0) + 1
                if args.verbose:
                    sev = SEVERITY.get(x.get("severity", 1), "?")
                    line = x["range"]["start"]["line"] + 1
                    print(f"    {sev} [{code}] L{line}: {x['message'].splitlines()[0]}")
    print(f"\ntotal: {grand_diags} diagnostics ({grand_errors} errors) "
          f"over {grand_files} files")
    if by_code:
        print("by code:")
        for code, count in sorted(by_code.items(), key=lambda kv: -kv[1]):
            print(f"  {count:6d}  {code or '(none)'}")
    return 1 if grand_diags else 0


def cmd_warm(args) -> int:
    # One indexing thread is far cheaper than a whole session, but they still
    # add up on a tree this size; scale them the same way and leave the same
    # headroom.
    jobs = args.jobs or max(1, min(auto_jobs(cap=(os.cpu_count() or 2)) * 2,
                                   (os.cpu_count() or 2)))
    rc = 0
    for tree in args.trees:
        if not os.path.isfile(os.path.join(cache_dir(tree), "compile_commands.json")):
            print(f"[{tree}] no database yet -- run gen first")
            rc = 1
            continue
        print(f"[{tree}] warming clangd's index ({jobs} threads), "
              f"currently {index_size(tree)}")
        started = time.time()
        ok = warm_index(tree, jobs, args.timeout)
        took = time.time() - started
        print(f"    {'done' if ok else 'TIMED OUT'} in {took:.0f}s -- "
              f"now {index_size(tree)}")
        if not ok:
            rc = 1
    return rc


def cmd_clean(args) -> int:
    for tree in args.trees:
        d = cache_dir(tree)
        if os.path.isdir(d):
            shutil.rmtree(d)
            print(f"removed {d}")
        write_tree_block(tree, None)
        print(f"removed the {tree} block from {USER_CONFIG}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Give clangd a kernel tree it can read, without writing "
                    "anything into that tree.  Driven by kbuildlab; the tree "
                    "directories are named, never searched for.")
    ap.add_argument("--tree", action="append", default=[], metavar="DIR",
                    help="a tree directory to act on (repeatable)")
    ap.add_argument("--peer", action="append", default=[], metavar="DIR",
                    help="another tree to know about but not act on; used to "
                         "borrow scripts/clang-tools/gen_compile_commands.py "
                         "for a tree too old to ship one (repeatable)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("gen", help="build the database and write the clangd config")
    g.add_argument("-f", "--force", action="store_true",
                   help="rebuild compile_commands.json even if one is cached")
    g.add_argument("--no-overlay", action="store_true",
                   help="skip the annotated Kconfig/linker-script headers")
    g.add_argument("--no-header-context", action="store_true",
                   help="skip per-header database entries (headers then parse alone)")
    g.add_argument("--header-diagnostics", action="store_true",
                   help="report diagnostics inside headers too; the include context "
                        "makes most of them real, but a handful of headers the build "
                        "force-includes still parse twice and report on themselves")
    g.set_defaults(func=cmd_gen)

    c = sub.add_parser("check", help="measure diagnostics through a real clangd session")
    c.add_argument("-n", "--number", type=int, default=120, help="sources to sample")
    c.add_argument("-j", "--jobs", type=int, default=0,
                   help="parallel clangd sessions; 0 (default) picks a number "
                        "from free memory and adjusts if it runs short")
    c.add_argument("-s", "--seed", type=int, default=1)
    c.add_argument("-v", "--verbose", action="store_true")
    c.add_argument("--all", action="store_true",
                   help="scan every file in the database instead of a sample")
    c.add_argument("--journal", metavar="PREFIX",
                   help="append each result to PREFIX.<tree>.jsonl as it lands")
    c.add_argument("--resume", action="store_true",
                   help="with --journal, skip files already recorded")
    c.set_defaults(func=cmd_check)

    w = sub.add_parser("warm", help="build clangd's background index now")
    w.add_argument("-j", "--jobs", type=int, default=0,
                   help="clangd indexing threads; 0 (default) picks a number "
                        "from free memory")
    w.add_argument("--timeout", type=float, default=7200.0,
                   help="give up after this many seconds (default 2h)")
    w.set_defaults(func=cmd_warm)

    k = sub.add_parser("clean", help="undo everything gen wrote for these trees")
    k.set_defaults(func=cmd_clean)

    args = ap.parse_args()
    if not args.tree:
        raise SystemExit("clangd.py: --tree DIR is required")
    for d in args.peer:
        register_tree(d)
    acting = []
    for d in args.tree:
        name = register_tree(d)
        if name is None:
            raise SystemExit(f"clangd.py: not a tree (no tree.conf, or no kernel "
                             f"source inside): {d}")
        acting.append(name)
    args.trees = acting
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
