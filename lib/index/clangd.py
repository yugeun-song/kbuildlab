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

Seven things break clangd on a GCC-built kernel tree.  Each is fixed by
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
     red.  Every option a unit uses is probed against the real clang, for the
     target that unit compiles for, and only what clang rejects is dropped
     from its entry.  Probing is what keeps this right across trees and
     configs: -mabi=lp64 is wrong on arm64 and correct on riscv64, and nobody
     has to remember that.

  3. Units aimed at another machine.  x86_64 compiles its boot stub as i386,
     riscv64 builds a 32-bit compat vDSO, and every tree builds host tools.
     Each entry names the build compiler's own target, as the build image
     reports it, and keeps the options that move it (-m16, -march=rv32ima);
     clang is asked which options those are, so no directory is named here.
     A file built more than once -- lib/string.c, again for an early-boot
     stub -- is represented by its typical build, read off the build's output.
     A unit that undefines a Kconfig symbol compiles against a config of its
     own, so it decides how a file reads only where nothing else reads it.

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

  7. A header's #if groups judged from the wrong macros.  The preamble built
     for a header is not the state the build had on reaching it: the reaching
     file may have defined something first, and the preamble may run past the
     point of inclusion.  inactive_regions.py replays the header's context source and
     restores the difference; `regions` measures what clangd greys out against
     what the build compiler compiled, unit by unit, inside the build image.

  8. clang answering for itself.  __clang__, __GNUC__ and the __has_* tests
     are the compiler's identity, so clangd takes compiler-clang.h where the
     build took compiler-gcc.h.  The build compiler is asked what the tree's
     conditionals ask, once per flag set, since a sanitizer flag or -m32 moves
     the answers; they lead every command line wherever clang can follow them.
     Only what a condition evaluates is answered: a macro reached only from
     code, such as clang's spelling of GCC's __seg_gs keyword, is left alone.

Nothing here names a directory, a header or a macro of a particular tree: the
include paths, the arch directory, the header floor, the targets and the
compiler's own headers are read from the build's units and the build compiler
every run, so a changed config or option is followed by the next gen.

Everything named "unused" is off: the compiler's -Wunused family, clangd's own
unused-include signals, and the clang-tidy checks for unused parameters and
declarations.

`check` measures the result the way an editor sees it: a real clangd LSP
session, didOpen per file, counting published diagnostics.  clangd --check is
not the verdict -- it reports errors only and stays silent about every warning
that still lands in the editor's gutter.  It is used only as a second opinion,
for files clangd publishes nothing about at all.

Usage (kbuildlab supplies the paths and --build-shell; --peer lets a tree too
old to ship scripts/clang-tools/gen_compile_commands.py borrow one from a newer
tree):
    clangd.py --tree DIR [--peer DIR ...] --build-shell CMD gen [-f] [--no-build-image]
    clangd.py --tree DIR check [--all] [-n N] [-j N] [--journal PREFIX]
    clangd.py --tree DIR regions [--all] [-n N] [-S N] [FILE ...]
    clangd.py --tree DIR clean
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import fcntl
import hashlib
import inspect
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
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(1, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import kbuildinfo
import inactive_regions
import treeinfo

# An empty XDG variable counts as unset, as it does in lib/common.sh.
CACHE = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"),
                     "kbuildlab", "clangd")
USER_CONFIG = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
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
    """SRC in the tree's tree.conf (lib/common.sh kbl_tree_src)."""
    src = treeinfo.norm_src(tree_conf_get(tree_dir, "SRC"))
    return src if src and os.path.isdir(src) else None


def tree_conf_get(tree_dir: str, key: str) -> str | None:
    """kbl_tree_get's grammar (lib/common.sh), held to it by tests/tree-conf-vectors.json."""
    return treeinfo.conf_get(tree_dir, key)


def write_text(path: str, text: str) -> None:
    """Replace a file clangd may be reading in one step."""
    with open(path + ".kbl-tmp", "w") as f:
        f.write(text)
    os.replace(path + ".kbl-tmp", path)


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
    """The build's own entries, one per file, as kbuild wrote them.

    kbuild's generator reruns when forced or when no units were kept; the
    database is rebuilt from the kept units every time, so every later step
    starts from what kbuild recorded rather than from its own last output.
    """
    ksrc = kernel_root(tree)
    out_dir = cache_dir(tree)
    os.makedirs(out_dir, exist_ok=True)
    units_path = os.path.join(out_dir, UNITS_FILE)
    assembly = 0
    if force or not os.path.exists(units_path):
        gen = find_generator(tree)
        tmp = units_path + ".tmp"
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
        assembly = len(entries) - len(kept)
        with open(units_path, "w") as f:
            json.dump(kept, f)
    units = build_units(tree)
    # A file built twice -- the boot stub's video code, once more for the
    # realmode wakeup with -D_WAKEUP -- has two entries, and clangd reads one.
    # The first stands in until the build's readings say which build of the
    # file is the typical one (pick_units).
    first: dict[str, dict] = {}
    for e in units:
        first.setdefault(e["file"], e)
    write_cdb(tree, list(first.values()))
    return len(first), assembly + len(units) - len(first)


def cdb_entries(tree: str) -> list[dict]:
    with open(os.path.join(cache_dir(tree), "compile_commands.json")) as f:
        return json.load(f)


def write_cdb(tree: str, entries: list[dict]) -> None:
    """Replace the database in one step, since clangd may read it at any time."""
    path = os.path.join(cache_dir(tree), "compile_commands.json")
    with open(path + ".tmp", "w") as f:
        json.dump(entries, f, indent=1)
    os.replace(path + ".tmp", path)


UNITS_FILE = "units.json"
SHELL_FILE = "build_shell.json"


def build_units(tree: str) -> list[dict]:
    """Every C compile of the last build, one per object, as kbuild ran it."""
    path = os.path.join(cache_dir(tree), UNITS_FILE)
    if not os.path.isfile(path):
        raise SystemExit(f"{path} is missing: run gen for {tree} first")
    with open(path) as f:
        return json.load(f)


def unit_deps(unit: dict, symbols: bool = False) -> set[str] | None:
    """What kbuild recorded one unit as reading, tree-relative; None without a record."""
    args = entry_args(unit)
    if "-o" not in args[:-1]:
        return None
    obj = os.path.join(unit["directory"], args[args.index("-o") + 1])
    return kbuild_deps(os.path.join(os.path.dirname(obj), f".{os.path.basename(obj)}.cmd"),
                       symbols)


def unit_reads(ksrc: str, unit: dict, symbols: bool = False) -> list[str] | None:
    """What kbuild recorded one unit as reading, absolute; None without a record."""
    deps = unit_deps(unit, symbols)
    return None if deps is None else [os.path.normpath(os.path.join(ksrc, x)) for x in deps]


def built(e: dict) -> bool:
    """A compile of the build's own, as opposed to an entry made here for a
    file the build only included, which names its language before the file."""
    args = entry_args(e)
    return len(args) < 3 or args[-3] != "-x"


def build_shell(tree: str, given: str | None, keep: bool = False) -> list[str]:
    """How to run a command in the tree's build image, as kbuildlab stated it.

    gen is handed it and keeps it, so a later regions run reaches the same image.
    """
    path = os.path.join(cache_dir(tree), SHELL_FILE)
    if given:
        shell = shlex.split(given)
        if keep:
            os.makedirs(cache_dir(tree), exist_ok=True)
            with open(path, "w") as f:
                json.dump(shell, f)
        return shell
    if not os.path.isfile(path):
        raise SystemExit(f"no build shell for {tree}: pass --build-shell, or run "
                         f"`kbuildlab tags {tree}`, which supplies it")
    with open(path) as f:
        return json.load(f)


def overlay_dir(tree: str) -> str:
    return os.path.join(cache_dir(tree), "overlay")


def standins(tree: str, entries: list[dict]) -> dict[str, str]:
    """Overlay copy -> the tree header it shadows on this build's include path."""
    ov = overlay_dir(tree)
    ksrc = kernel_root(tree)
    dirs: list[str] = []
    for e in entries:
        for a in entry_args(e):
            if a.startswith("-I"):
                d = os.path.normpath(os.path.join(ksrc, a[2:]))
                if d not in dirs:
                    dirs.append(d)
    found: dict[str, str] = {}
    for root, _, names in os.walk(ov):
        for n in names:
            copy = os.path.join(root, n)
            rel = os.path.relpath(copy, ov)
            for d in dirs:
                cand = os.path.join(d, rel)
                if d != ov and os.path.isfile(cand):
                    found[copy] = cand
                    break
    return found


def forced_includes(args: list[str]) -> list[str]:
    return [args[i + 1] for i, a in enumerate(args[:-1]) if a == "-include"]


_FORWARD_COUNTED_BY = re.compile(
    r"^#define (\w+)\((\w+)\) __attribute__\(\(__counted_by__\(\2\)\)\)$", re.M)


def write_compat_shim(tree: str, entries: list[dict],
                      view: inactive_regions.View) -> tuple[str, str | None, int]:
    """Redefine empty the attribute macros this clang cannot parse the tree's
    use of.  Which macros, and which forced include defines them, is read from
    preprocessing the build's most common forced-include sequence, so both
    follow the tree's version and configuration.

    Returns the shim, the forced include it has to follow, and the macro count.
    """
    path = os.path.join(cache_dir(tree), "shim", "clang_compat.h")

    def forced(e: dict) -> tuple:
        return tuple(i for i in forced_includes(entry_args(e)) if i != path)

    if kbuildinfo.clang_handles_forward_counted_by():
        shutil.rmtree(os.path.dirname(path), ignore_errors=True)
        return path, None, 0
    tally: dict[tuple, int] = {}
    for e in entries:
        tally[forced(e)] = tally.get(forced(e), 0) + 1
    seq = max(tally, key=lambda k: tally[k])
    e = next(x for x in entries if forced(x) == seq)
    argv = view.flags(entry_args(e), e["file"])
    bare = []
    skip = False
    for a in argv:
        if skip or a == "-include":
            skip = not skip
            continue
        bare.append(a)

    def defined_after(count: int) -> list[str]:
        includes = [x for inc in seq[:count] for x in ("-include", inc)]
        r = subprocess.run(bare + includes + ["-E", "-dM", "-x", "c", os.devnull],
                           cwd=e["directory"], capture_output=True, text=True)
        return sorted({m.group(1) for m in _FORWARD_COUNTED_BY.finditer(r.stdout)})

    names = defined_after(len(seq))
    if not names:
        shutil.rmtree(os.path.dirname(path), ignore_errors=True)
        return path, None, 0
    after = next(seq[n - 1] for n in range(1, len(seq) + 1) if defined_after(n) == names)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    write_text(path, "/* GCC resolves counted_by against the finished struct; this clang does not. */\n"
               + "".join(f"#undef {n}\n#define {n}(member)\n" for n in names))
    return path, after, len(names)


def apply_overlay(tree: str, shim: str | None = None, after: str | None = None) -> int:
    """Put the annotated headers ahead of the tree's own on the include path,
    and the compatibility shim right behind the forced include it amends.

    -I is searched in order, so the entry goes in front of the kernel's own -I
    rather than being appended, which is why this edits the database instead of
    using the config's CompileFlags.Add.
    """
    ov = overlay_dir(tree)
    if not os.path.isdir(ov):
        return 0
    cdb = os.path.join(cache_dir(tree), "compile_commands.json")
    with open(cdb) as f:
        entries = json.load(f)
    flag = f"-I{ov}"

    touched = 0
    for e in entries:
        args = entry_args(e)
        changed = False
        if flag not in args:
            args.insert(1, flag)
            changed = True
        if shim and shim not in args:
            at = [i + 2 for i, a in enumerate(args[:-1])
                  if a == "-include" and args[i + 1] == after]
            if at:
                args[at[0]:at[0]] = ["-include", shim]
                changed = True
        if not changed:
            continue
        if "command" in e:
            e["command"] = shlex.join(args)
        else:
            e["arguments"] = args
        touched += 1
    if touched:
        write_cdb(tree, entries)
    return touched


def option_groups(words: list[str]) -> list[tuple[str, ...]]:
    """Options with their separate values.  The build compiled one input, so
    any other word that is not an option belongs to the option before it:
    -o obj, -include file, --param asan-globals=1."""
    out: list[list[str]] = []
    for w in words:
        if w.startswith("-") or not out:
            out.append([w])
        else:
            out[-1].append(w)
    return [tuple(g) for g in out]


def options(args: list[str]) -> list[tuple[str, ...]]:
    """A compile command's options with their values, less input, output,
    dependency files, forced includes, include paths and defines."""
    return [g for g in option_groups(args[1:-1])
            if g[0] not in ("-o", "-MF", "-MT", "-MQ", "-include", "-imacros", "-x",
                            "-c", "-MD", "-MMD")
            and not g[0].startswith(("-D", "-I", "-U", "-Wp,-M", "-isystem", "-iquote",
                                     "-idirafter"))]


def signature(args: list[str]) -> tuple:
    """What decides a unit's target and predefined macros: its driver and its
    options, warnings aside."""
    return (args[0],) + tuple(w for g in options(args) if not g[0].startswith("-W") for w in g)


KEPT_FILE = "kept.json"


def identity_dir(tree: str) -> str:
    return os.path.join(cache_dir(tree), "identity")


def identity_kept(tree: str) -> tuple[set[str], set[str]]:
    """Tests left to clang, and the macros that hang on them."""
    path = os.path.join(identity_dir(tree), KEPT_FILE)
    if not os.path.isfile(path):
        return set(), set()
    with open(path) as f:
        kept = json.load(f)
    return set(kept["tests"]), set(kept["names"])


def build_read(tree: str, units: list[dict]) -> list[str]:
    """Every tree file kbuild recorded a unit as reading, and the units' own."""
    ksrc = kernel_root(tree)
    read: set[str] = set()
    for u in units:
        read.update(unit_reads(ksrc, u) or ())
        read.add(os.path.normpath(u["file"]))
    return sorted(p for p in read if p.startswith(ksrc + "/"))


def ask_build_image(shell: list[str], drivers: list[str], flag_sets: list[tuple],
                    questions: str) -> dict:
    """The build compiler's own answers, from inside the build image."""
    spec = json.dumps({"drivers": drivers, "flag_sets": [list(f) for f in flag_sets],
                       "questions": questions})
    r = subprocess.run(shell + ["python3", os.path.abspath(inactive_regions.__file__), "ask"],
                       input=spec, capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f"the build image could not be asked ({shlex.join(shell)}):\n"
                         f"{r.stderr[-1500:]}")
    return json.loads(r.stdout)


def write_identities(tree: str, targets: "Targets", flag_sets: list[tuple], answers: list,
                     questions: tuple[str, list], closure: set[str], own: set,
                     read: list[str]) -> tuple[dict[tuple, str], dict]:
    """Per flag set, a forced include holding the build compiler's answers
    where clang's differ; flag sets that answer alike share one file."""
    d = identity_dir(tree)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    ask, pairs = questions
    paths: dict[tuple, str] = {}
    meta = {"macros": set(), "followed": set(), "kept": set(), "redefined": set()}
    for sig, (bpre, bans) in zip(flag_sets, answers):
        if bpre is None or bans is None:
            continue
        argv = targets.argv(sig)
        clang = tuple(subprocess.run(argv + ["-E", "-dM", "-x", "c", src], input=ask,
                                     capture_output=True, text=True).stdout
                      for src in (os.devnull, "-"))
        header, m = inactive_regions.identity((bpre, bans), clang, closure, pairs, own)
        for k in meta:
            meta[k].update(m[k])
        if header:
            path = os.path.join(d, hashlib.sha1(header.encode()).hexdigest()[:16] + ".h")
            if not os.path.exists(path):
                write_text(path, header)
            paths[sig] = path
    kept = sorted(meta["kept"])
    with open(os.path.join(d, KEPT_FILE), "w") as f:
        json.dump({"tests": kept, "names": inactive_regions.kept_names(read, kept) if kept else []},
                  f, indent=0)
    return paths, {k: sorted(v) for k, v in meta.items()}


def entry_args(e: dict) -> list[str]:
    cmd = e.get("command")
    return shlex.split(cmd) if cmd else list(e["arguments"])


_INCLUDE_RE = re.compile(r'^[ \t]*#[ \t]*include[ \t]*[<"]([^>"]+)[>"]', re.M)
_INCLUDE_KIND_RE = re.compile(r'^[ \t]*#[ \t]*include[ \t]*([<"])([^>"]+)[>"]', re.M)

HEADER_SUFFIXES = (".h", ".hpp", ".hh", ".hxx", ".inc")


def is_header(path: str) -> bool:
    return path.endswith(HEADER_SUFFIXES)


CONTEXT_FILE = "header_context.json"


def kbuild_deps(cmd_file: str, symbols: bool = False) -> set[str] | None:
    """The headers fixdep recorded for one object, tree-relative; None when the
    object has no dependency list to consult.

    With symbols, also the file Kconfig touches whenever a symbol the object
    tests changes.  fixdep takes autoconf.h out of every list and names those
    files instead, so they are how kbuild knows to rebuild after a config
    change that edits no header, and how a result kept for the object knows
    it is stale."""
    try:
        with open(cmd_file, encoding="utf-8", errors="replace") as f:
            lines = f.read().split("\n")
    except OSError:
        return None
    deps: set[str] = set()
    listing = False
    for line in lines:
        if line.startswith("deps_"):
            listing = True
            continue
        if not listing:
            continue
        word = line.strip().rstrip("\\").strip()
        if not word:
            break
        if not word.startswith("$("):
            deps.add(os.path.normpath(word))
        elif symbols and word.startswith("$(wildcard ") and word.endswith(")"):
            deps.add(os.path.normpath(word[len("$(wildcard "):-1].strip()))
    return deps or None


def header_entries(tree: str, entries: list[dict], verbose: bool, floor: str,
                   truth: "Compilers | None" = None) -> tuple[list[dict], dict[str, str]]:
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

    With truth, a file the build compiled takes its context only from a source
    whose unit compiled it the way that shows the most of it, and one no
    #include line leads to still gets such a source.
    """
    ksrc = kernel_root(tree)
    by_source: dict[str, dict] = {os.path.relpath(e["file"], ksrc): e for e in entries}

    # How each file includes things, in order.  Read once; the kernel is large
    # but this is a single linear pass over the files already in the database
    # plus the headers they reach.
    inc_cache: dict[str, list[str] | None] = {}

    # A quoted include is looked up beside the file that names it before the
    # include path, as the compiler does; one found only there is spelled by
    # its tree path, since a forced include is looked up from the tree's root.
    def includes_of(rel: str) -> list[str] | None:
        if rel in inc_cache:
            return inc_cache[rel]
        try:
            with open(os.path.join(ksrc, rel), encoding="utf-8", errors="replace") as f:
                found = _INCLUDE_KIND_RE.findall(f.read())
        except OSError:
            inc_cache[rel] = None
            return None
        got = []
        for quote, spelling in found:
            local = os.path.normpath(os.path.join(os.path.dirname(rel), spelling))
            beside = (quote == '"' and not local.startswith("..")
                      and os.path.isfile(os.path.join(ksrc, local)))
            got.append(local if beside and resolve(spelling) != local else spelling)
        inc_cache[rel] = got
        return got

    # Resolve an include spelling to a tree-relative path through the -I list
    # the build's units search, then the tree root, where the spellings made
    # below for includes found only beside their includer are rooted.
    search = include_dirs(tree) + ["."]

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

    # An #include line is not an inclusion: half of them sit under an #ifdef
    # this configuration leaves out.  kbuild recorded what GCC really read for
    # each object, so a source that read the header outranks one that only
    # names it; the latter is still better than nothing for a header no object
    # in this configuration reads.
    read_by: dict[str, set[str] | None] = {}
    proven: set[str] = set()

    def really_reads(source: str, target: str) -> bool:
        if source not in read_by:
            read_by[source] = unit_deps(by_source[source])
        deps = read_by[source]
        return deps is not None and target in deps

    def offer(target: str, source: str, prefix: list[str]) -> None:
        if truth and truth.ok(source, target) is False:
            return
        real = really_reads(source, target)
        cur = context.get(target)
        if cur is not None and (target in proven) != real:
            if not real:
                return
        elif cur is not None and len(prefix) <= len(cur[1]):
            return
        context[target] = (source, prefix[-MAX_PREFIX:])
        if real:
            proven.add(target)

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
    # context, extended by whatever it included first.  Each pass reaches one
    # level deeper and can only add a header or lengthen a capped prefix, so
    # repeating until neither happens converges.
    while True:
        before = (len(context), sum(len(p) for _, p in context.values()))
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
        if (len(context), sum(len(p) for _, p in context.values())) == before:
            break
    if truth:
        for rel in truth.compiled():
            if rel not in context and rel not in by_source:
                source = truth.source_for(rel, by_source)
                if source:
                    context[rel] = (source, [])

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
        if base is None or header in by_source:
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
        # context: what most of the build's sources include first.
        chain = [] if pure_macro(header) else (prefix or [floor])
        for spelling in chain:
            if spelling != header and resolve(spelling):
                out += ["-include", spelling]
        out += ["-x", "c-header" if is_header(header) else "c", header]
        made.append({"directory": ksrc, "file": os.path.join(ksrc, header),
                     "command": shlex.join(out)})
    if verbose:
        print(f"    {len(made)} headers given the include context their users supply"
              f" ({dropped_self} self-including entries pruned)")
    return made, {h: source for h, (source, _) in context.items()
                  if source in by_source and h not in by_source}


# ---------------------------------------------------------------------------
# what each unit compiles for, and the flags clang takes from it
# ---------------------------------------------------------------------------

# clangd reads a target off the driver's name: aarch64-linux-gnu-gcc-16 means
# --target=aarch64-linux-gnu.  Used only when the build image is not asked;
# otherwise the build compiler's own -dumpmachine names it.
_TRIPLE_RE = re.compile(r"^([A-Za-z0-9_]+-[A-Za-z0-9_]+(?:-[A-Za-z0-9_.]+)*)-"
                        r"(?:gcc|clang|cc)(?:-[0-9.]+)?$")


def driver_triple(driver: str) -> str | None:
    m = _TRIPLE_RE.match(os.path.basename(driver))
    return m.group(1) if m else None


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


class Targets:
    """Per unit: the target it compiled for, and the options clang refuses.

    The base is the build compiler's own target.  Options such as -m16 or
    -march=rv32ima move it; clang is asked which options do and where they
    take it, so a 32-bit boot stub or compat vDSO needs no path named here.
    Every other option is tried once, in the setting it is used in, and kept
    only if clang takes it without a word.
    """

    def __init__(self, base: dict[str, str | None], units: list[dict], jobs: int):
        self.base = base
        fd, probe = tempfile.mkstemp(suffix=".c", prefix="clangd_probe_")
        with os.fdopen(fd, "w") as f:
            f.write("int probe_translation_unit;\n")
        try:
            self._settle([entry_args(u) for u in units], jobs, probe)
        finally:
            os.unlink(probe)

    def _clang(self, driver: str) -> list[str]:
        b = self.base.get(driver)
        return ["clang"] + ([f"--target={b}"] if b else [])

    def _settle(self, commands: list[list[str]], jobs: int, probe: str) -> None:
        def triple(job):
            driver, flags = job
            r = subprocess.run(self._clang(driver) + list(flags) + ["-print-effective-triple"],
                               capture_output=True, text=True)
            return job, (r.stdout.strip() if r.returncode == 0 else None)

        def refused(job):
            driver, moving, flag = job
            r = subprocess.run(self._clang(driver) + list(moving) + ["-fsyntax-only", *flag, probe],
                               capture_output=True, text=True)
            return job, (r.returncode != 0 or bool(_REJECT_ERROR.search(r.stderr))
                         or bool(_REJECT_WARNING.search(r.stderr)))

        flag_sets = sorted({signature(a) for a in commands})
        with cf.ThreadPoolExecutor(jobs) as ex:
            singles = {(sig[0], ()) for sig in flag_sets}
            singles |= {(sig[0], g) for sig in flag_sets for g in option_groups(list(sig[1:]))}
            where = dict(ex.map(triple, sorted(singles)))
            self.moving = {sig: tuple(w for g in option_groups(list(sig[1:]))
                                      if where[(sig[0], g)] not in (None, where[(sig[0], ())])
                                      for w in g)
                           for sig in flag_sets}
            tries = set()
            for a in commands:
                sig = signature(a)
                tries |= {(a[0], self.moving[sig], g) for g in options(a)
                          if not set(g) <= set(self.moving[sig])}
            self.verdicts = dict(ex.map(refused, sorted(tries)))
            self.effective = dict(ex.map(triple, sorted({(sig[0], self.moving[sig])
                                                         for sig in flag_sets})))

    def refused(self, args: list[str]) -> set[tuple[str, ...]]:
        sig = signature(args)
        return {g for g in options(args) if self.verdicts.get((args[0], self.moving[sig], g))}

    def base_of(self, args: list[str]) -> str | None:
        return self.base.get(args[0])

    def where(self, args: list[str]) -> str | None:
        sig = signature(args)
        return self.effective.get((sig[0], self.moving[sig]))

    def argv(self, sig: tuple) -> list[str]:
        """clang for a flag set, as the database entries run it."""
        return self._clang(sig[0]) + [w for g in option_groups(list(sig[1:]))
                                      if not self.verdicts.get((sig[0], self.moving[sig], g))
                                      for w in g]


def rewrite_entries(tree: str, units: list[dict], targets: Targets,
                    identities: dict[tuple, str], compiler_include: dict[str, str],
                    quiet: set[str]) -> int:
    """Turn each build command into the command clangd should run: the unit's
    target named, the flags clang refuses there dropped, the identity include
    first, and a -isystem naming the build compiler's own headers pointed at
    clang's instead, since each compiler can only read its own."""
    resource = os.path.join(subprocess.run(["clang", "-print-resource-dir"], capture_output=True,
                                           text=True, check=True).stdout.strip(), "include")
    cdb = os.path.join(cache_dir(tree), "compile_commands.json")
    with open(cdb) as f:
        entries = json.load(f)
    for e in entries:
        args = entry_args(e)
        sig = signature(args)
        base, refused = targets.base_of(args), targets.refused(args)
        own = compiler_include.get(args[0])

        def system(d: str) -> str | None:
            if d == own:
                return resource
            return d if os.path.isdir(d) else resource if own is None else None

        out = [args[0]] + ([f"--target={base}"] if base else [])
        if sig in identities:
            out += ["-include", identities[sig]]
        for g in option_groups(args[1:-1]):
            if g[0] == "-isystem" and len(g) == 2:
                d = system(g[1])
                out += ["-isystem", d] if d else []
            elif g[0].startswith("-isystem") and len(g) == 1:
                d = system(g[0][len("-isystem"):])
                out += [f"-isystem{d}"] if d else []
            elif g not in refused:
                out += g
        out.append(args[-1])
        if os.path.normpath(e["file"]) in quiet:
            out[-1:-1] = INTRINSIC_QUIET
        e.pop("arguments", None)
        e["command"] = shlex.join(out)
    write_cdb(tree, entries)
    return len(entries)


def include_dirs(tree: str) -> list[str]:
    """The -I directories most units search, in their order, tree-relative."""
    if "include_dirs" not in TREES[tree]:
        ksrc = kernel_root(tree)
        tally: dict[tuple, int] = {}
        for u in build_units(tree):
            dirs = []
            for a in entry_args(u):
                if a.startswith("-I") and len(a) > 2:
                    d = os.path.relpath(os.path.normpath(os.path.join(u["directory"], a[2:])), ksrc)
                    if not d.startswith("..") and d not in dirs:
                        dirs.append(d)
            tally[tuple(dirs)] = tally.get(tuple(dirs), 0) + 1
        TREES[tree]["include_dirs"] = list(max(tally, key=lambda k: tally[k])) if tally else []
    return TREES[tree]["include_dirs"]


# ---------------------------------------------------------------------------
# config fragments
# ---------------------------------------------------------------------------

# "too many errors emitted, stopping now" is a fatal, not a diagnostic, so
# Diagnostics.Suppress never reaches it -- it is the one thing still shown on a
# tree whose diagnostics are otherwise fully suppressed.  Lifting the cap removes
# it at its source.
ERROR_LIMIT = ["-ferror-limit=0"]

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
    # Uninitialized-value analysis rests on the compiler seeing every write to
    # a variable.  In a kernel that premise does not hold.  The write often
    # happens somewhere the translation unit cannot contain and the compiler
    # was never going to read: a bootloader handing over a register block,
    # firmware filling a table before the kernel is entered, an assembly stub,
    # a linker script, another CPU.  Some of it is not C at all.
    #
    # What the analysis produces on this kind of code is a report about a write
    # it could not see, which is not the same claim as a write that does not
    # happen -- and telling those apart needs knowledge of the boot path that
    # no single-translation-unit analysis has.  The family is off.
    "-Wno-uninitialized",
    "-Wno-sometimes-uninitialized",
    "-Wno-conditional-uninitialized",
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

# What a header reports about being parsed twice rather than about anything
# written in it.  clangd ignores a header's own include guard when that header
# is the file being edited -- otherwise opening one would show an empty buffer
# -- so a header the build force-includes gets parsed again as the main file
# and every declaration in it is a redefinition of itself.  Named separately
# from the blanket suppression so --header-diagnostics is worth using.
SELF_REPARSE_DIAGNOSTICS = [
    "redefinition",
    "redefinition_different_typedef",
    "err_redefinition_different_kind",
    "typedef_redefinition",
    "duplicate_member",
    "static_non_static",
    "pp_including_mainfile_in_preamble",
    "fatal_too_many_errors",
    "macro-redefined",
    "builtin-macro-redefined",
]

INTRINSIC_QUIET = [
    "-Wno-incompatible-pointer-types",
    "-Wno-int-conversion",
    "-Wno-incompatible-function-pointer-types",
]


def compiler_header_users(readings: inactive_regions.Readings,
                          compiler_include: dict[str, str]) -> set[str]:
    """Units that read their compiler's own headers, from where the build
    compiler says those live.  clang's versions are not GCC's -- its
    arm_neon.h types uint64_t as unsigned long long where GCC's says unsigned
    long -- so code using them reads as a wall of type errors in a tree the
    build compiled cleanly; those units take INTRINSIC_QUIET."""
    users = set()
    for n, u in enumerate(readings.units):
        own = compiler_include.get(entry_args(u)[0])
        if own and any(p.startswith(own.rstrip("/") + "/") for p in readings.outside[n]):
            users.add(os.path.normpath(u["file"]))
    return users

_WNO_RE = re.compile(r"-Wno-[a-z0-9-]+")


def refused_flags(argv: list[str], flags, jobs: int) -> set[str]:
    """The flags clang, run as argv, answers with an error or a warning."""
    fd, probe = tempfile.mkstemp(suffix=".c", prefix="clangd_probe_")
    with os.fdopen(fd, "w") as f:
        f.write("int probe_translation_unit;\n")

    def one(flag: str) -> tuple[str, bool]:
        r = subprocess.run(argv + ["-fsyntax-only", flag, probe], capture_output=True, text=True)
        return flag, (r.returncode != 0 or bool(_REJECT_ERROR.search(r.stderr))
                      or bool(_REJECT_WARNING.search(r.stderr)))

    try:
        with cf.ThreadPoolExecutor(jobs) as ex:
            return {flag for flag, bad in ex.map(one, sorted(flags)) if bad}
    finally:
        os.unlink(probe)


def kernel_warning_opinions(argv: list[str], jobs: int) -> list[str]:
    """Every -Wno- the kernel's own global makefiles mention, kept only where
    this clang knows it.

    Global means the top Makefile, kbuild's scripts/Makefile*, and each tree's
    arch Makefiles, which is where kbuild states flags for every object --
    including the ones it only passes when built by clang.  Read from every
    tree in the workspace, not just this one: a kernel from 2016 predates the
    kernel's clang support and so has no way to say which clang warnings it
    considers noise on its own source, but a newer tree next to it says exactly
    that about code that has barely changed.
    """
    found: set[str] = set()
    for name in discover_trees():
        ksrc = kernel_root(name)
        try:
            arch = [os.path.join("arch", srcarch(name))]
        except SystemExit:
            arch = []
        paths = ["Makefile"]
        for d in ["scripts"] + arch:
            if os.path.isdir(os.path.join(ksrc, d)):
                paths += [os.path.join(d, n) for n in sorted(os.listdir(os.path.join(ksrc, d)))
                          if n.startswith("Makefile")]
        for rel in paths:
            try:
                with open(os.path.join(ksrc, rel), encoding="utf-8", errors="replace") as f:
                    found.update(_WNO_RE.findall(f.read()))
            except OSError:
                continue
    found.discard("-Wno-unknown-warning-option")
    return sorted(found - refused_flags(argv, found, jobs))

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


def unparseable_sources(tree: str, entries: list[dict], verbose: bool) -> list[str]:
    """Sources sitting in a built directory that this tree cannot parse.

    Most of what a tree does not build still reads fine: a driver switched off
    in .config is the same C, and clangd borrows a neighbour's command for it.
    The ones that do not read are the ones asking for headers this tree does
    not have -- drivers/macintosh wanting powerpc's asm/, block/partitions/ibm.c
    wanting s390's, the build's own host tools wanting stdio.h through a
    -nostdinc command line.  There is no command that makes those parse here,
    so they get the same treatment as a foreign arch directory: navigation
    kept, diagnostics dropped.

    Found by checking whether each include resolves, rather than by matching
    names: `arm64.c` is a giveaway but `ibm.c` is not, and the question that
    actually matters is whether the file's dependencies exist in this tree.
    Only directories that contain something the tree does build are searched,
    which is where the problem shows up and keeps this to seconds.
    """
    ksrc = kernel_root(tree)
    built = {e["file"] for e in entries}
    search = include_dirs(tree) + ["."]
    out: list[str] = []
    for d in sorted({os.path.dirname(f) for f in built}):
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for n in names:
            if not n.endswith((".c", ".h")):
                continue
            path = os.path.join(d, n)
            if path in built:
                continue
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    incs = _INCLUDE_RE.findall(f.read(40000))
            except OSError:
                continue
            for spelling in incs:
                if os.path.isfile(os.path.join(d, spelling)):
                    continue
                if any(os.path.isfile(os.path.join(ksrc, sd, spelling)) for sd in search):
                    continue
                out.append(os.path.relpath(path, ksrc))
                break
    if verbose and out:
        print(f"    {len(out)} source(s) in built directories need headers this "
              f"tree does not have")
    return sorted(out)


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


def srcarch(tree: str) -> str:
    """The arch directory whose headers the build searches: kbuild's SRCARCH,
    which is not always tree.conf's ARCH -- x86_64 builds arch/x86."""
    for d in include_dirs(tree):
        parts = d.split("/")
        if len(parts) == 3 and parts[0] == "arch" and parts[2] == "include":
            return parts[1]
    raise SystemExit(f"{tree}: no unit searches an arch/*/include directory")


def header_floor(tree: str, entries: list[dict]) -> str:
    """The header most of the build's sources include first: what the tree
    assumes is present before anything else."""
    tally: dict[str, int] = {}
    for e in entries:
        try:
            with open(e["file"], encoding="utf-8", errors="replace") as f:
                found = _INCLUDE_KIND_RE.search(f.read(65536))
        except OSError:
            continue
        while found and found.group(1) != "<":
            found = _INCLUDE_KIND_RE.search(found.string, found.end())
        if found:
            tally[found.group(2)] = tally.get(found.group(2), 0) + 1
    if not tally:
        raise SystemExit(f"{tree}: no source of the build includes a header")
    return max(tally, key=lambda k: tally[k])


def identity_quiet(ident: bool, meta: dict) -> list[str]:
    """Warnings the identity include causes by design rather than finds: its
    redefinition of clang's feature-test macros, and the attributes only the
    build compiler has, which clang meets on the build compiler's paths and
    ignores."""
    quiet = []
    if meta.get("redefined"):
        quiet.append("-Wno-builtin-macro-redefined")
    if ident:
        quiet.append("-Wno-unknown-attributes")
    return quiet


def tree_fragments(tree: str, entries: list[dict], verbose: bool, floor: str,
                   warn_argv: list[str], read: list[str], jobs: int,
                   header_diagnostics: bool = False, quiet: list[str] = ()) -> list[str]:
    """The tree's blocks of clangd policy.  What each file is compiled with --
    its target, the flags clang takes -- is already in its database entry;
    what is left here is which warnings and checks are noise, and which paths
    no command line can make parse."""
    ksrc = kernel_root(tree)
    prefix = re.escape(ksrc) + "/"
    # The overlay's copies are opened too -- go-to-definition lands on them --
    # and they take the tree's flags, so they take the tree's fragments.
    lab = (f"({re.escape(ksrc)}|{re.escape(overlay_dir(tree))})/"
           if os.path.isdir(overlay_dir(tree)) else prefix)
    add = list(ERROR_LIMIT + DRIVER_QUIET + UNUSED_QUIET) + CLANG_ONLY_NOISE + list(quiet)
    own_opinions = kernel_warning_opinions(warn_argv, jobs)
    add += [w for w in own_opinions if w not in add]
    if verbose:
        print(f"    {len(own_opinions)} warning suppressions taken from the kernels' makefiles")

    frags = []
    head = [
        f"# {tree}",
        "If:",
        f"  PathMatch: {path_regex(lab, '.*')}",
        "CompileFlags:",
        f"  CompilationDatabase: {cache_dir(tree)}",
        "  Add:",
        indent(add, "    ").rstrip("\n"),
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

    # Source this tree never compiles or reads.  An arch it is not configured
    # for has no correct parse to reach through this command line, and neither
    # have tools/ or samples/, which are built by something else entirely.
    # Silencing them costs nothing real: navigation, hover and completion still
    # work, and the diagnostics that go away could only ever have been wrong.
    used = {os.path.relpath(p, ksrc).split("/")[0] for p in read}
    used |= {os.path.relpath(e["file"], ksrc).split("/")[0] for e in entries}
    compiled_arches = {os.path.relpath(e["file"], ksrc).split("/")[1] for e in entries
                       if os.path.relpath(e["file"], ksrc).startswith("arch/")}
    own = srcarch(tree)
    dead: list[str] = []
    arch_dir = os.path.join(ksrc, "arch")
    if os.path.isdir(arch_dir):
        for a in sorted(os.listdir(arch_dir)):
            if not os.path.isdir(os.path.join(arch_dir, a)) or a == own:
                continue
            # An arch with sources in the database (arm under arm64, for its
            # shared xen code) still has headers written for a machine this
            # tree is not; only those are silenced.
            dead.append(f"arch/{a}/include/" if a in compiled_arches else f"arch/{a}/")
    for top in sorted(os.listdir(ksrc)):
        if os.path.isdir(os.path.join(ksrc, top)) and not top.startswith(".") and top not in used:
            dead.append(f"{top}/")
    stray = unparseable_sources(tree, entries, verbose)

    if dead or stray:
        if verbose:
            not_built = [d for d in dead if not d.startswith("arch/")]
            print(f"    {len(dead)} paths this tree never compiles are silenced "
                  f"({len(dead) - len(not_built)} foreign arch, "
                  f"{len(not_built)} non-kernel: {', '.join(sorted(not_built)[:6])})")
        # Directories match everything under them; individual files match
        # themselves.  Built separately rather than sharing one suffix, so a
        # file entry cannot accidentally match a longer path that starts the
        # same way.
        pat = "|".join([re.escape(d) + ".*" for d in dead]
                       + [re.escape(s) for s in stray])
        frags.append("\n".join([
            f"# {tree}: not built here -- no command line makes this parse correctly",
            "If:",
            f"  PathMatch: {path_regex(prefix, '(', pat, ')')}",
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
    # asm/atomic.h got there first.  A floor in front -- what most of the
    # tree's sources include first -- supplies most of that context, which is
    # what makes hover and go-to-definition work inside a header at all; but
    # "most" is not "all", and what is left over is a diagnostic about the
    # reading, never about the header.  So the context is forced and the
    # diagnostics are dropped.
    hdr = [
        f"# {tree}: headers get build context; what still fails is the reading, not the header",
        "If:",
        f"  PathMatch: {path_regex(lab, r'.*\.(h|hpp|hh|hxx|inc)')}",
        "CompileFlags:",
        "  Add:",
        "    - -include",
        f"    - {floor}",
        "    - -Wno-macro-redefined",
        "    - -Wno-builtin-macro-redefined",
    ]
    # Measured both ways.  With header diagnostics on, a 640-file sample
    # reports 40: headers no source in this tree includes (a driver switched
    # off in .config), and fragments meant to be pasted in after macros are set
    # -- include/asm-generic/audit_*.h, include/trace/*.h.  Neither can be given
    # the context it needs; the first because nothing recorded one, the second
    # because no ordering makes it a translation unit.
    #
    # So silent by default.  --header-diagnostics turns them on for someone
    # editing a header, and the self-reparse artefacts stay suppressed either
    # way, since those are never about the code.
    hdr += ["    - -ferror-limit=0", "Diagnostics:", "  Suppress:"]
    hdr += [f"    - {c}" for c in SELF_REPARSE_DIAGNOSTICS]
    if not header_diagnostics:
        hdr += ["    - '*'"]
    frags.append("\n".join(hdr) + "\n")
    return frags


def read_user_config() -> str:
    if os.path.isfile(USER_CONFIG):
        with open(USER_CONFIG) as f:
            return f.read()
    return ""


WORKSPACE_FRAGMENT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  os.pardir, os.pardir, "presets", "clangd", "workspace.yaml")


def write_workspace_block(workspace: str) -> None:
    """Materialise presets/clangd/workspace.yaml for this workspace.

    Keyed by the workspace path, so it cannot collide with a tree's own block.
    """
    if not os.path.isfile(WORKSPACE_FRAGMENT):
        return
    with open(WORKSPACE_FRAGMENT) as f:
        body = f.read().replace("@WORKSPACE@", re.escape(os.path.abspath(workspace)))
    write_tree_block(f"workspace {os.path.abspath(workspace)}", [body])


def write_tree_block(tree: str, blocks: list[str] | None) -> None:
    """Replace one tree's marked block, leaving every other tree -- and
    anything the operator wrote themselves -- untouched.

    Per-tree markers are what makes `kbuildlab tags v6.12-arm64` safe to run:
    the other trees' fragments are not regenerated, so a tree whose database
    has not been rebuilt keeps the configuration that matches it.  Passing
    None removes the block.
    """
    begin, end = block_markers(tree)
    body = ""
    if blocks:
        body = begin + "\n" + "\n---\n".join(blocks) + end + "\n"
    os.makedirs(os.path.dirname(USER_CONFIG), exist_ok=True)
    # Trees are indexed in parallel.  The read-modify-write holds a lock on the
    # config's own directory, the one thing every writer of it shares.
    lock = os.open(os.path.dirname(USER_CONFIG), os.O_RDONLY)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        existing = read_user_config()
        if begin in existing and end in existing:
            pre = existing.split(begin)[0]
            post = existing.split(end, 1)[1].lstrip("\n")
            new = pre + body + post
        elif body:
            new = (existing.rstrip("\n") + "\n---\n" + body) if existing.strip() else body
        else:
            return
        new = re.sub(r"\n---\s*\n(\s*---\s*\n)+", "\n---\n", new)
        new = re.sub(r"\A(\s*---\s*\n)+", "", new)
        if new.strip():
            write_text(USER_CONFIG, new)
        elif os.path.isfile(USER_CONFIG):
            os.unlink(USER_CONFIG)
    finally:
        os.close(lock)


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


PREPROCESS_MB = 160        # measured: clang -E on kernel/sched/core.c plus its reader


def auto_jobs(cap: int | None = None, reserve_mb: int = RESERVE_MB,
              job_mb: int = SESSION_MB) -> int:
    cpus = os.cpu_count() or 2
    ceiling = cap or max(1, cpus - 1)
    av = available_mb()
    if av is None:
        return min(2, ceiling)
    return max(1, min(ceiling, (av - reserve_mb) // job_mb))


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
        self.inactive: dict[str, list] = {}
        self.silent = 0
        self.lock = threading.Lock()
        self.arrived = threading.Event()
        threading.Thread(target=self._read_loop, daemon=True).start()
        self._request("initialize", {
            "processId": os.getpid(),
            "rootUri": "file://" + root,
            "capabilities": {"textDocument": {
                "publishDiagnostics": {},
                "inactiveRegionsCapabilities": {"inactiveRegions": True}}},
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
                # clangd percent-encodes what it sends back -- the comma in
                # qcom,gcc-sm6115.h -- so replies are keyed by the decoded URI.
                elif msg.get("method") == "textDocument/publishDiagnostics":
                    p = msg["params"]
                    self.diags[urllib.parse.unquote(p["uri"])] = p["diagnostics"]
                    self.arrived.set()
                elif msg.get("method") == "textDocument/inactiveRegions":
                    p = msg["params"]
                    self.inactive[urllib.parse.unquote(p["textDocument"]["uri"])] = p["regions"]
                    self.arrived.set()
                elif "id" in msg:
                    self._write({"jsonrpc": "2.0", "id": msg["id"], "result": None})

    def diagnose(self, path: str, timeout: float = 20.0) -> list:
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
            # clangd publishes nothing at all for a file that has nothing to
            # say, and a kernel tree has thousands of them: every dt-bindings
            # header is bare #defines with no code to diagnose.  Nudge it once
            # with an edit, briefly, and then take silence for what it is.
            #
            # The earlier version of this ran `clangd --check` as a second
            # opinion before giving up.  It was correct and it was ruinous:
            # 200 seconds per silent file, thousands of them, and a scan that
            # spent an hour moving six files.  Cross-checked instead by hand at
            # the time -- every silent file --check was asked about came back
            # with zero errors -- so silence is recorded as silence.
            self._notify("textDocument/didChange", {
                "textDocument": {"uri": uri, "version": 2},
                "contentChanges": [{"text": text + "\n"}]})
            deadline = time.time() + 5
            while time.time() < deadline:
                with self.lock:
                    if uri in self.diags:
                        result = self.diags[uri]
                        break
                self.arrived.wait(0.2)
                self.arrived.clear()
        self._notify("textDocument/didClose", {"textDocument": {"uri": uri}})
        if result is None:
            with self.lock:
                self.silent += 1
            return []
        return result

    def regions(self, path: str, timeout: float = 120.0) -> set[int] | None:
        """Lines clangd greys out in this file, 1-based; None if it never said."""
        uri = "file://" + path
        with self.lock:
            self.inactive.pop(uri, None)
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
        self.arrived.clear()
        self._notify("textDocument/didOpen", {"textDocument": {
            "uri": uri, "languageId": "c", "version": 1, "text": text}})
        deadline = time.time() + timeout
        got = None
        while time.time() < deadline and got is None:
            with self.lock:
                got = self.inactive.get(uri)
            if got is None:
                self.arrived.wait(0.2)
                self.arrived.clear()
        self._notify("textDocument/didClose", {"textDocument": {"uri": uri}})
        if got is None:
            return None
        dead: set[int] = set()
        for r in got:
            dead.update(range(r["start"]["line"] + 1, r["end"]["line"] + 2))
        return dead

    def close(self) -> None:
        try:
            self._request("shutdown", None, wait=5)
            self._notify("exit", None)
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()


_MACRO_ONLY_RE = re.compile(r"^[ \t]*[^ \t\n#]", re.M)


def has_code(path: str) -> bool:
    """Is there anything here but preprocessor directives?

    A header of bare #defines -- every file under include/dt-bindings/, and a
    good many register-definition headers -- has no declarations to diagnose,
    and clangd publishes nothing at all for it: not an empty list, nothing.
    Waiting on that is waiting for a message the server was never going to
    send, at twenty seconds a file and a thousand files a tree.  They are
    excluded from a scan rather than waited on, which costs no coverage: a file
    with no code has no diagnostics to find.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read(200000)
    except OSError:
        return True
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"//[^\n]*", "", text)
    return bool(_MACRO_ONLY_RE.search(text))


def all_files(tree: str) -> list[str]:
    """Every file in the database that has code in it."""
    named = sorted({e["file"] for e in cdb_entries(tree)})
    return [f for f in named if not is_header(f) or has_code(f)]


def sample_files(tree: str, n: int, seed: int) -> list[str]:
    """A deterministic spread over sources and headers: n sources at random,
    one from each of up to n directories the build compiles in, and the first
    headers of the largest directories under the include paths it searches."""
    ksrc = kernel_root(tree)
    sources = sorted(e["file"] for e in cdb_entries(tree))
    rng = random.Random(seed)
    picked = set(rng.sample(sources, min(n, len(sources))))
    strata: dict[str, list[str]] = {}
    for f in sources:
        strata.setdefault(os.path.dirname(os.path.relpath(f, ksrc)), []).append(f)
    for d in rng.sample(sorted(strata), min(n, len(strata))):
        picked.add(rng.choice(strata[d]))
    headers: dict[str, list[str]] = {}
    for top in include_dirs(tree):
        root = os.path.join(ksrc, top)
        for sub in sorted(os.listdir(root)) if os.path.isdir(root) else ():
            d = os.path.join(root, sub)
            if os.path.isdir(d):
                headers[d] = sorted(x for x in os.listdir(d) if x.endswith(".h"))
    per_dir = max(2, n // 10)
    for d in sorted(headers, key=lambda k: -len(headers[k]))[:per_dir]:
        picked.update(os.path.join(d, x) for x in headers[d][:per_dir])
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
    files.  A tree already warm costs only reading the project back.

    Driven through the LSP progress notifications rather than by watching the
    cache directory: clangd says when it starts and when it has nothing left,
    and guessing from file timestamps would either stop early or never stop.
    Reading the project back is itself work clangd reports, so the end comes
    even when every shard is current.  No timer stands in for it: on the
    smallest tree and one shared core, clangd began after 70s and found every
    shard current after 216s, and a timer that ran out first called a stale
    index warm.
    """
    root = kernel_root(tree)
    # clangd finds the database only through the tree's block in the user
    # configuration; without it nothing would ever start.
    if block_markers(tree)[0] not in read_user_config():
        if not quiet:
            print(f"    {USER_CONFIG} has no block for {tree} -- run gen first")
        return False
    seed = next((e["file"] for e in cdb_entries(tree)
                 if not is_header(e["file"]) and os.path.isfile(e["file"])), None)
    if seed is None:
        if not quiet:
            print(f"    no source in {tree}'s database exists to open")
        return False
    proc = subprocess.Popen(
        ["clangd", "--background-index", f"-j={jobs}", "--log=error",
         "--background-index-priority=normal"],
        cwd=root, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL)
    state = {"done": False, "last": "", "replies": {}}
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
                    if kind == "report":
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
    with open(seed, encoding="utf-8", errors="replace") as f:
        text = f.read()
    write({"jsonrpc": "2.0", "method": "textDocument/didOpen", "params": {
        "textDocument": {"uri": "file://" + seed, "languageId": "c",
                         "version": 1, "text": text}}})

    started = time.time()
    last_shown = 0.0
    ok = False
    try:
        while time.time() - started < timeout:
            with lock:
                done, last = state["done"], state["last"]
            if done:
                ok = True
                break
            if proc.poll() is not None:
                break           # clangd is gone; nothing will finish now
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
    jobs = args.jobs or auto_jobs(job_mb=PREPROCESS_MB)
    for tree in args.trees:
        print(f"[{tree}]")
        if args.fresh and os.path.isdir(cache_dir(tree)):
            shutil.rmtree(cache_dir(tree))
            print(f"    discarded {cache_dir(tree)}")
        kept, dropped = gen_cdb(tree, args.force)
        print(f"    compile_commands.json: {kept} entries"
              + (f" ({dropped} assembly or repeated entries dropped)" if dropped else "")
              + f"  -> {cache_dir(tree)}")
        ksrc = kernel_root(tree)
        units = build_units(tree)
        tally: dict[tuple, int] = {}
        for u in units:
            sig = signature(entry_args(u))
            tally[sig] = tally.get(sig, 0) + 1
        flag_sets = sorted(tally)
        drivers = sorted({f[0] for f in flag_sets})
        read = build_read(tree, units)
        closure, ops = inactive_regions.questions(read)
        resource = os.path.join(subprocess.run(["clang", "-print-resource-dir"], capture_output=True,
                                               text=True, check=True).stdout.strip(), "include")
        _, theirs = inactive_regions.questions(
            sorted(os.path.join(resource, n) for n in os.listdir(resource) if n.endswith(".h")))
        own = {(op, a) for op, found in theirs.items() for a in found - ops.get(op, set())}
        for op, a in own:
            ops.setdefault(op, set()).add(a)
        questions = inactive_regions.question_text(ops)
        readings = truth = None
        compiler_include: dict[str, str] = {}
        if args.no_build_image:
            base = {d: driver_triple(d) for d in drivers}
            answers = None
        else:
            shell = build_shell(tree, args.build_shell, keep=True)
            facts = ask_build_image(shell, drivers, flag_sets, questions[0])
            base = {d: facts["drivers"][d][0] or None for d in drivers}
            compiler_include = {d: facts["drivers"][d][1] for d in drivers if facts["drivers"][d][1]}
            answers = facts["answers"]
            print("    every unit through the build compiler", flush=True)
            readings, nunits, carried, failed = build_readings(tree, shell, jobs)
            apart = count_own_config(tree, readings)
            truth = Compilers(ksrc, readings)
            print(f"    {nunits} units read: {carried} carried over"
                  + (f", {len(failed)} refused" if failed else "")
                  + f"; {len(readings.files)} files compiled with #if groups")
            if apart:
                print(f"    {apart} unit(s) undefine a Kconfig symbol and so compile against a "
                      f"config of their own; they decide how a file reads only where "
                      f"nothing else reads it")
            moved = pick_units(tree, truth)
            if moved:
                print(f"    {moved} file(s) built more than once take their typical build, "
                      f"not the one kbuild's generator found first")
        targets = Targets(base, units, jobs)
        per_target: dict[str, list] = {}
        for u in units:
            cmd = entry_args(u)
            slot = per_target.setdefault(targets.where(cmd) or "clang's default", [0, set()])
            slot[0] += 1
            slot[1] |= targets.refused(cmd)
        for where, (count, refused) in sorted(per_target.items(), key=lambda kv: -kv[1][0]):
            print(f"    target {where}: {count} units, {len(refused)} flag(s) clang refuses there")
        identities, meta = {}, {}
        if answers is not None:
            identities, meta = write_identities(tree, targets, flag_sets, answers, questions,
                                                closure, own, read)
        else:
            shutil.rmtree(identity_dir(tree), ignore_errors=True)
        quiet = compiler_header_users(readings, compiler_include) if readings else set()
        n = rewrite_entries(tree, units, targets, identities, compiler_include, quiet)
        if identities:
            print(f"    identity: {len(meta['macros'])} predefined macro(s) and "
                  f"{len(meta['followed'])} feature test(s) answered as the build compiler "
                  f"answers them, in {len(set(identities.values()))} forced include(s) over "
                  f"{sum(n for sig, n in tally.items() if sig in identities)} units")
        if meta.get("kept"):
            print(f"    identity: {len(meta['kept'])} feature test(s) left to clang, which "
                  f"lacks what the build compiler has: {', '.join(meta['kept'])}")
        if quiet:
            print(f"    {len(quiet)} unit(s) read the build compiler's own headers")
        view = inactive_regions.View(ksrc)
        if not args.no_overlay:
            kbuildinfo.build_overlay(ksrc, overlay_dir(tree), srcarch(tree), verbose=True)
            shim, after, neutralised = write_compat_shim(
                tree, [e for e in cdb_entries(tree) if built(e)], view)
            n = apply_overlay(tree, shim if neutralised else None, after)
            if n:
                print(f"    overlay put ahead of the tree on {n} command lines"
                      + (f", {neutralised} counted_by macro(s) defined away behind "
                         f"{after}" if neutralised else ""))
        src = [e for e in cdb_entries(tree) if built(e)]
        floor = header_floor(tree, src)
        print(f"    header floor: {floor}, what most sources include first")
        if not args.no_header_context:
            extra, chosen = header_entries(tree, src, verbose=True, floor=floor, truth=truth)
            if not args.no_macro_state:
                by_rel = {os.path.relpath(e["file"], ksrc): e for e in src}
                done = inactive_regions.settle(
                    view, cache_dir(tree), by_rel, extra, chosen, standins(tree, src), floor,
                    entry_args, jobs, verbose=True,
                    reads=lambda source: unit_reads(ksrc, by_rel[source], symbols=True))
                astray = [h for h in done["empty"] if truth and h in truth.readings.index]
                if astray:
                    print(f"    {len(astray)} header(s) the build compiles compile nothing of "
                          f"themselves under clang in their source: {', '.join(astray[:5])}")
            with open(os.path.join(cache_dir(tree), CONTEXT_FILE), "w") as f:
                json.dump(chosen, f, indent=0, sort_keys=True)
            by_file = {e["file"]: e for e in extra}
            for copy, orig in standins(tree, src).items():
                if orig in by_file:
                    argv = entry_args(by_file[orig])
                    extra.append({"directory": ksrc, "file": copy,
                                  "command": shlex.join(argv[:-1] + [copy])})
            write_cdb(tree, src + extra)
        in_tree = [e for e in cdb_entries(tree) if e["file"].startswith(ksrc + "/")]
        common = max(tally, key=lambda f: tally[f])
        blocks = tree_fragments(tree, in_tree, True, floor, targets.argv(common), read, jobs,
                                header_diagnostics=args.header_diagnostics,
                                quiet=identity_quiet(bool(identities), meta))
        write_tree_block(tree, blocks)
        print(f"    {len(blocks)} config fragments -> {USER_CONFIG}")
        if args.workspace:
            write_workspace_block(args.workspace)
            print(f"    workspace fragment -> {USER_CONFIG}")
        print(f"    nothing was written inside {ksrc}")
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


REGION_SAMPLE = (30, 120)  # sources, included files


def config_for(tree: str) -> str:
    """The parts of the user config that decide how this tree is read."""
    text = read_user_config()
    kept = []
    for m in re.finditer(r"^# >>> kbuildlab clangd: (.*?) >>>\n.*?^# <<< kbuildlab clangd: \1 <<<$",
                         text, re.M | re.S):
        if m.group(1) == tree or m.group(1).startswith("workspace "):
            kept.append(m.group(0))
    return "\n".join(kept)


def build_readings(tree: str, shell: list[str], jobs: int):
    """Every distinct reading of each file the build compiled, over all units,
    and how many units there were, were carried over and were refused."""
    ksrc = kernel_root(tree)
    units = build_units(tree)
    memo = inactive_regions.Memo(os.path.join(cache_dir(tree), "readings"),
                                 code=inactive_regions.collector_code())
    got = inactive_regions.Readings(units)
    shown = [0.0]

    def progress(done: int, total: int) -> None:
        if done == total or time.time() - shown[0] > 30:
            shown[0] = time.time()
            print(f"      {done}/{total} units preprocessed", flush=True)

    failed = inactive_regions.readings(shell, ksrc, units, memo, jobs,
                                       lambda u: unit_reads(ksrc, u, symbols=True), got.take,
                                       progress)
    memo.prune()
    return got, len(units), memo.hits, failed


_UNDEF_CONFIG = re.compile(r"^[ \t]*#[ \t]*undef[ \t]+CONFIG_(\w+)", re.M)


def altered_config_units(tree: str, readings: inactive_regions.Readings) -> set[int]:
    """Units that compile against a config of their own: they compile an
    #undef of a Kconfig symbol, as a 32-bit vDSO does to pass for a 32-bit
    kernel and a decompressor does to leave paravirt out.  What only they
    compile is not what the tree's config compiles."""
    ksrc = kernel_root(tree)
    symbols: set[str] | None = None
    lines_of: dict[str, tuple[list[int], inactive_regions.Layout]] = {}
    reads = []
    for u in readings.units:
        mine = {os.path.relpath(u["file"], ksrc)} | {
            d for d in unit_deps(u) or () if not os.path.isabs(d)}
        reads.append(mine)
        for rel in mine - lines_of.keys():
            try:
                with open(os.path.join(ksrc, rel), encoding="utf-8", errors="replace") as f:
                    text = f.read()
            except OSError:
                lines_of[rel] = ([], None)
                continue
            found = [(m.start(), m.group(1)) for m in _UNDEF_CONFIG.finditer(text)]
            if found and symbols is None:
                symbols = set(kbuildinfo.index_kconfig(ksrc))
            hits = [text.count("\n", 0, at) + 1 for at, name in found if name in (symbols or ())]
            lines_of[rel] = (hits, inactive_regions.Layout(text) if hits else None)
    altered = set()
    for n, mine in enumerate(reads):
        undefs = [rel for rel in mine if lines_of[rel][0]]
        if not undefs:
            continue
        live = readings.live(n)
        for rel in undefs:
            hits, lay = lines_of[rel]
            if any(lay.owner[no] < 0 or lay.owner[no] in live.get(rel, ()) for no in hits):
                altered.add(n)
                break
    return altered


def count_own_config(tree: str, readings: inactive_regions.Readings) -> int:
    altered = altered_config_units(tree, readings)
    readings.discount(altered)
    return len(altered)


class Compilers:
    """Which units compile each file the way that shows the most of it, and
    which build of a file built more than once is the typical one."""

    def __init__(self, ksrc: str, readings: inactive_regions.Readings):
        self.ksrc, self.readings = ksrc, readings
        best = {}
        for fi, rel in enumerate(readings.files):
            if len(readings.count[fi]) == 1:
                best[fi] = 0
                continue
            with open(os.path.join(ksrc, rel), encoding="utf-8", errors="replace") as f:
                best[fi] = readings.best(rel, inactive_regions.Layout(f.read()))
        self.bits = readings.compilers(best)
        units_of: dict[str, list[int]] = {}
        for n, u in enumerate(readings.units):
            units_of.setdefault(os.path.relpath(u["file"], ksrc), []).append(n)
        # A build of a file stands for it when it reads the file itself the
        # best way, then when it reads the largest share of everything it
        # reads the way most units do: lib/string.c, not the copy an early-boot
        # stub compiles with -D__DISABLE_EXPORTS.
        self.unit_of = {rel: max(ns, key=lambda n: self._typical(n, rel))
                        for rel, ns in units_of.items()}

    def _typical(self, n: int, rel: str) -> tuple:
        fi = self.readings.index.get(rel)
        own = fi is None or bool(self.bits[n][fi >> 3] >> (fi & 7) & 1)
        share = (int.from_bytes(self.bits[n], "little").bit_count()
                 / max(1, len(self.readings.file_of[n])))
        return own, share, -n

    def compiled(self) -> list[str]:
        return self.readings.files

    def unit_for(self, source: str) -> dict | None:
        n = self.unit_of.get(source)
        return None if n is None else self.readings.units[n]

    def ok(self, source: str, target: str) -> bool | None:
        """Whether the build that stands for source compiles target the best
        way; None when the build compiled no #if group of target."""
        fi = self.readings.index.get(target)
        if fi is None:
            return None
        n = self.unit_of.get(source)
        return n is not None and bool(self.bits[n][fi >> 3] >> (fi & 7) & 1)

    def source_for(self, target: str, sources) -> str | None:
        fi = self.readings.index[target]
        for n, bits in enumerate(self.bits):
            if bits[fi >> 3] >> (fi & 7) & 1:
                rel = os.path.relpath(self.readings.units[n]["file"], self.ksrc)
                if rel in sources and self.unit_of.get(rel) == n:
                    return rel
        return None


def pick_units(tree: str, truth: Compilers) -> int:
    """Put the typical build of each file in the database in place of the
    first one kbuild's generator found; returns how many changed."""
    ksrc = kernel_root(tree)
    entries = cdb_entries(tree)
    moved = 0
    for i, e in enumerate(entries):
        u = truth.unit_for(os.path.relpath(e["file"], ksrc))
        if u is not None and entry_args(u) != entry_args(e):
            entries[i] = u
            moved += 1
    if moved:
        write_cdb(tree, entries)
    return moved


def verdict(lay: inactive_regions.Layout, readings: list[tuple], greyed: set[int],
            lacking: tuple[set[str], set[str]] = (set(), set())):
    """(status, lines shaded though compiled, lines lit though skipped, lines
    compiled only under another reading, a unit that compiles those).  A file
    shown as some units compile it but not as most do counts its lines against
    the usual reading, and names a unit that reads it so."""
    lits = [(lay.lit(set(r)), unit) for r, _, unit in readings]
    union = set().union(*(lit for lit, _ in lits))
    lit = lay.visible - greyed
    if lit == union:
        return "exact", 0, 0, 0, None
    other = next((unit for L, unit in lits if L - lit), None)
    if any(L == lit for L, _ in lits):
        best, unit = lits[inactive_regions.best_reading([L for L, _ in lits],
                                                        [n for _, n, _ in readings])]
        if best != lit:
            return "a less common reading", len(best - lit), len(lit - best), len(union - lit), unit
        return "one of several readings", 0, 0, len(union - lit), other
    closest = min((L for L, _ in lits), key=lambda L: len(L ^ lit))
    status = "differs"
    wrong = (closest ^ lit) | (lit - union)
    if lacking[0] and all(lay.owner[no] >= 0 and inactive_regions.depends_on_chain(
            lay, lay.owner[no], *lacking) for no in wrong):
        status = "needs what clang lacks"
    return (status, len(closest - lit), len(lit - union), len(union - closest - lit), other)


def content_digest(path: str) -> str | None:
    try:
        with open(path, "rb") as f:
            return hashlib.sha1(f.read()).hexdigest()
    except OSError:
        return None


def audit_regions(tree: str, rels: list[str], readings: inactive_regions.Readings, jobs: int,
                  fresh: bool = False) -> tuple[list[tuple], int]:
    """Compare clangd's inactive regions with the build's readings.

    What clangd shades is kept under a digest of its command, the config,
    clangd itself and what the file's context read, and asked for again only
    when one of those changed; the readings side is recomputed every time.
    """
    ksrc = kernel_root(tree)
    by_rel: dict[str, dict] = {}
    for e in cdb_entries(tree):
        by_rel.setdefault(os.path.relpath(e["file"], ksrc), e)
    context_path = os.path.join(cache_dir(tree), CONTEXT_FILE)
    if not os.path.isfile(context_path):
        raise SystemExit(f"{context_path} is missing: run gen for {tree} first")
    with open(context_path) as f:
        chosen = json.load(f)
    with open(os.path.join(cache_dir(tree), "compile_commands.json"), "rb") as f:
        cdb_digest = hashlib.sha1(f.read()).hexdigest()
    memo = inactive_regions.Memo(os.path.join(cache_dir(tree), "verdicts"),
                                 code=inspect.getsource(Session.regions))
    clangd_id = subprocess.run(["clangd", "--version"], capture_output=True,
                               text=True, check=True).stdout
    config = config_for(tree)

    def key_of(rel: str) -> str:
        path = os.path.join(ksrc, rel)
        e = by_rel.get(rel)
        if e is None:
            return memo.key("greyed", cdb_digest, clangd_id, config, inactive_regions.Memo.stamp({path}))
        source = e if built(e) else by_rel.get(chosen.get(rel))
        read = unit_reads(ksrc, source, symbols=True) if source else None
        # Forced includes are written afresh by every gen, so they count by
        # content: a regeneration that changed nothing re-asks nothing.
        own = sorted(x for x in forced_includes(entry_args(e)) if os.path.isabs(x))
        return memo.key("greyed", entry_args(e), clangd_id, config,
                        inactive_regions.Memo.stamp(set(read or ()) | {path}),
                        [[x, content_digest(x)] for x in own])

    lacking = identity_kept(tree)
    greyed: dict[str, list[int] | None] = {}
    keys = {rel: key_of(rel) for rel in rels}
    work: queue.Queue = queue.Queue()
    for rel in rels:
        kept = None if fresh else memo.get(keys[rel])
        if kept is not None:
            greyed[rel] = kept
        else:
            work.put(rel)
    carried = len(greyed)
    lock = threading.Lock()

    def worker():
        session = Session(ksrc)
        try:
            while True:
                try:
                    rel = work.get_nowait()
                except queue.Empty:
                    return
                seen = session.regions(os.path.join(ksrc, rel))
                with lock:
                    greyed[rel] = None if seen is None else sorted(seen)
                    if seen is not None:
                        memo.put(keys[rel], sorted(seen))
        finally:
            session.close()

    threads = [threading.Thread(target=worker, daemon=True)
               for _ in range(min(jobs, work.qsize()))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    rows = []
    for rel in rels:
        if greyed.get(rel) is None:
            rows.append((rel, "clangd reported nothing", 0, 0, 0, None))
            continue
        with open(os.path.join(ksrc, rel), encoding="utf-8", errors="replace") as f:
            lay = inactive_regions.Layout(f.read())
        seen = [(r, n, os.path.relpath(readings.units[u]["file"], ksrc))
                for r, n, u in readings.of(rel)]
        rows.append((rel, *verdict(lay, seen, set(greyed[rel]) & lay.visible, lacking)))
    return sorted(rows), carried


def cmd_regions(args) -> int:
    differing = 0
    for tree in args.trees:
        ksrc = kernel_root(tree)
        shell = build_shell(tree, args.build_shell)
        print(f"[{tree}] every unit through the build compiler")
        readings, nunits, carried, failed = build_readings(
            tree, shell, args.jobs or auto_jobs(job_mb=PREPROCESS_MB))
        print(f"    {nunits} units: {carried} carried over, "
              f"{nunits - carried - len(failed)} preprocessed"
              + (f", {len(failed)} refused" if failed else ""))
        apart = count_own_config(tree, readings)
        if apart:
            print(f"    {apart} unit(s) compile against a config of their own and count only "
                  f"for files nothing else reads")
        for unit, err in failed:
            last = err.strip().splitlines()[-1] if err.strip() else "no output"
            print(f"      refused {os.path.relpath(unit['file'], ksrc)}: {last}")
        rels = sorted(readings.files)
        compiled = set(rels)
        # A source is a file some unit compiles as itself; everything else was
        # read through another file: headers, and .c files another includes.
        own = {os.path.relpath(u["file"], ksrc) for u in build_units(tree)}
        if args.paths:
            unknown = [p for p in args.paths if p not in compiled]
            if unknown:
                raise SystemExit("not read by the build, or without conditionals: "
                                 + ", ".join(unknown))
            chosen = list(args.paths)
        else:
            rng = random.Random(args.seed)
            all_src = [r for r in rels if r in own]
            all_inc = [r for r in rels if r not in own]
            want_src = args.sources if args.sources is not None else (
                len(all_src) if args.all else REGION_SAMPLE[0])
            want_inc = args.number if args.number is not None else (
                len(all_inc) if args.all else REGION_SAMPLE[1])
            chosen = (rng.sample(all_src, min(want_src, len(all_src)))
                      + rng.sample(all_inc, min(want_inc, len(all_inc))))
        sources = sorted(r for r in chosen if r in own)
        included = sorted(r for r in chosen if r not in own)
        jobs = auto_jobs(args.jobs or None)
        print(f"[{tree}] inactive regions: {len(sources)} sources, {len(included)} included "
              f"files, {jobs} clangd session(s)", flush=True)
        rows, kept = audit_regions(tree, sources + included, readings, jobs, args.fresh)
        if kept:
            print(f"    {kept} clangd reading(s) carried over: nothing they depend on changed")
        for kind, names in (("sources", set(sources)), ("included", set(included))):
            tally: dict[str, int] = {}
            for r in rows:
                if r[0] in names:
                    tally[r[1]] = tally.get(r[1], 0) + 1
            print(f"    {kind}: " + (", ".join(f"{n} {k}" for k, n in sorted(tally.items()))
                                     or "none"))
        bad = [r for r in rows if r[1] == "differs"]
        differing += len(bad)
        print(f"    lines shaded though the build compiles them: {sum(r[2] for r in bad)}")
        print(f"    lines lit though the build skips them:       {sum(r[3] for r in bad)}")
        for rel, _, shaded, lit, _, _ in bad:
            print(f"      {rel}: {shaded} shaded, {lit} lit")
        rarer = [r for r in rows if r[1] == "a less common reading"]
        differing += len(rarer)
        if rarer:
            print(f"    files shown as fewer units compile them: {len(rarer)}")
            for rel, _, shaded, lit, _, unit in rarer:
                print(f"      {rel}: {shaded} shaded, {lit} lit against the usual reading, "
                      f"which {unit} compiles")
        lacking = [r for r in rows if r[1] == "needs what clang lacks"]
        if lacking:
            print(f"    lines that follow a test clang cannot answer as the build compiler "
                  f"does: {sum(r[2] + r[3] for r in lacking)}")
            for rel, _, shaded, lit, _, _ in lacking:
                print(f"      {rel}: {shaded} shaded, {lit} lit")
        elsewhere = [r for r in rows if r[4]]
        print(f"    lines compiled only under another reading:   {sum(r[4] for r in elsewhere)}"
              f" in {len(elsewhere)} file(s); one parse can show one reading")
        if args.verbose:
            for rel, _, _, _, other, unit in elsewhere:
                print(f"      {rel}: {other} line(s), compiled by {unit} among others")
            for rel, status, *_ in rows:
                if status == "clangd reported nothing":
                    print(f"      {rel}: {status}")
    return 1 if differing else 0


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
        print(f"    {'done' if ok else 'not finished'} in {took:.0f}s -- "
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
    ap.add_argument("--workspace", metavar="DIR", default="",
                    help="workspace the trees live in; its policy fragment "
                         "(presets/clangd/workspace.yaml) is written too")
    ap.add_argument("--build-shell", metavar="CMD",
                    help="the command prefix that runs a program in the tree's build "
                         "image, with the kernel source as its working directory and "
                         "stdin attached; gen keeps it for regions")
    ap.add_argument("--peer", action="append", default=[], metavar="DIR",
                    help="another tree to know about but not act on; used to "
                         "borrow scripts/clang-tools/gen_compile_commands.py "
                         "for a tree too old to ship one (repeatable)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("gen", help="build the database and write the clangd config")
    g.add_argument("-f", "--force", action="store_true",
                   help="rebuild compile_commands.json even if one is cached")
    g.add_argument("--fresh", action="store_true",
                   help="discard everything cached for the tree first: the database, the "
                        "readings and memos, and clangd's own index")
    g.add_argument("-j", "--jobs", type=int, default=0,
                   help="preprocessors run at once; 0 (default) picks a number from "
                        "free memory and cores")
    g.add_argument("--no-overlay", action="store_true",
                   help="skip the annotated Kconfig/linker-script headers")
    g.add_argument("--no-header-context", action="store_true",
                   help="skip per-header database entries (headers then parse alone)")
    g.add_argument("--no-build-image", action="store_true",
                   help="neither ask the build compiler nor read what it compiled: clang "
                        "answers __clang__, __GNUC__ and the __has_* tests for itself, and "
                        "header contexts come from #include lines alone")
    g.add_argument("--no-macro-state", action="store_true",
                   help="skip replaying each header's context source; a header's "
                        "#if groups are then judged from its preamble alone")
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

    r = sub.add_parser("regions", help="compare clangd's inactive regions with the "
                                       "groups the build's own preprocessing skips")
    r.add_argument("paths", nargs="*", metavar="FILE",
                   help="tree-relative files to compare instead of a sample")
    r.add_argument("-n", "--number", type=int,
                   help=f"included files -- headers, and .c files another includes -- "
                        f"to sample (default {REGION_SAMPLE[1]})")
    r.add_argument("-S", "--sources", type=int,
                   help=f"sources to sample (default {REGION_SAMPLE[0]})")
    r.add_argument("-s", "--seed", type=int, default=1)
    r.add_argument("-j", "--jobs", type=int, default=0,
                   help="parallel clangd sessions; 0 (default) picks a number "
                        "from free memory")
    r.add_argument("--all", action="store_true",
                   help="compare every file of a kind whose count is not given")
    r.add_argument("--fresh", action="store_true",
                   help="ask clangd again even where nothing a verdict depends on changed")
    r.add_argument("-v", "--verbose", action="store_true",
                   help="also name the files that could not be compared")
    r.set_defaults(func=cmd_regions)

    w = sub.add_parser("warm", help="build clangd's background index now")
    w.add_argument("-j", "--jobs", type=int, default=0,
                   help="clangd indexing threads; 0 (default) picks a number "
                        "from free memory")
    w.add_argument("--timeout", type=float, default=7200.0,
                   help="give up after this many seconds (default 2h)")
    w.set_defaults(func=cmd_warm)

    k = sub.add_parser("clean", help="undo everything gen wrote for these trees")
    k.set_defaults(func=cmd_clean)

    d = sub.add_parser("drop", help="remove a tree's block by name, for a tree that is "
                                    "no longer registered")
    d.add_argument("name")

    args = ap.parse_args()
    if args.cmd == "drop":
        write_tree_block(args.name, None)
        return 0
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
