#!/usr/bin/env python3
"""The tree as its build compiled it, for ctags, cscope and GNU global: lines no
unit compiled are blanked and files no unit read are emptied, line numbers kept.

    tagview.py --tree DIR --view DIR build --build-shell CMD [-j N]
    tagview.py --tree DIR --view DIR stage
    tagview.py --tree DIR --view DIR harvest (--keep | --drop)
    tagview.py --tree DIR --view DIR forget
    tagview.py outputs [ctags|cscope|gtags]

The view directory is kbuildlab's to choose; under it: upper/ (the copies), work/
(overlayfs), state/ (what one run records), view.json, published.json, readings/.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import clangd
import inactive_regions as ir

CMD_LINE = re.compile(r"^(?:saved)?cmd_(?P<target>\S+) := (?P<prefix>.* )(?P<file>[^ ]*\.[cS]) *(?:;|$)")
SOURCE_LINE = re.compile(r"^source_\S+ := (?P<file>\S+)[ \t]*$", re.M)
STATE = "view.json"
PUBLISHED = "published.json"
# What each indexer writes at the top of the tree: all a run stages, publishes
# and, when that indexer fails, withdraws.
INDEXES = {"ctags": ("tags",),
           "cscope": ("cscope.files", "cscope.out", "cscope.out.in", "cscope.out.po",
                      "cscope.in.out", "cscope.po.out"),
           "gtags": ("GTAGS", "GRTAGS", "GPATH")}
OUTPUTS = {name for names in INDEXES.values() for name in names}


class Memo(ir.Memo):
    def __init__(self, directory: str, fallback: str, code: str):
        super().__init__(directory, code)
        self.fallback = fallback

    def get_packed(self, key: str):
        got = super().get_packed(key)
        if got is not None:
            return got
        src, dst = os.path.join(self.fallback, key), os.path.join(self.dir, key)
        try:
            with open(src, "rb") as f:
                got = json.loads(zlib.decompress(f.read()))
        except (OSError, ValueError, zlib.error):
            return None
        try:
            os.link(src, dst)
        except OSError:
            self.put_packed(key, got)
        self.hits += 1
        return got


def recorded_units(ksrc: str) -> list[dict]:
    units = []
    for d, dirs, files in os.walk(ksrc):
        dirs[:] = [x for x in dirs if x != ".git"]
        for name in files:
            if not (name.startswith(".") and name.endswith(".cmd")):
                continue
            path = os.path.join(d, name)
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    head = f.read(1 << 16)
            except OSError:
                continue
            m = CMD_LINE.match(head.split("\n", 1)[0])
            s = SOURCE_LINE.search(head)
            if not m or not s or os.path.normpath(m["file"]) != os.path.normpath(s["file"]):
                continue
            src = os.path.realpath(os.path.join(ksrc, m["file"]))
            if not src.startswith(ksrc + os.sep) or not os.path.isfile(src):
                continue
            command = m["prefix"].replace("\\#", "#").replace("$(pound)", "#") + m["file"]
            if "-P" in shlex.split(command):
                command = shlex.join(a for a in shlex.split(command) if a != "-P")
            units.append({"directory": ksrc, "file": src, "command": command, "cmd": path,
                          "target": m["target"]})
    compilers = {shlex.split(u["command"])[0] for u in units
                 if u["target"].endswith(".o") and u["file"].endswith(".c")}
    return [u for u in units if shlex.split(u["command"])[0] in compilers]


def unit_files(ksrc: str, unit: dict) -> set[str] | None:
    deps = clangd.kbuild_deps(unit["cmd"])
    if deps is None:
        return None
    return {d for d in deps if not os.path.isabs(d) and not d.startswith("..")}


def forced_closure(shell: list[str], ksrc: str, units: list[dict]) -> set[str]:
    probes: dict[tuple, list[str]] = {}
    for u in units:
        argv = ir.unit_argv(u["command"])[:-5]
        forced = tuple(argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "-include")
        lang = "assembler-with-cpp" if u["file"].endswith(".S") else "c"
        probes.setdefault((argv[0], forced, lang), argv + ["-M", "-x", lang, os.devnull])
    r = subprocess.run(shell + ["python3", os.path.abspath(__file__), "probe"],
                       input=json.dumps(list(probes.values())), capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f"the build image could not list the forced includes:\n{r.stderr[-1500:]}")
    out: set[str] = set()
    for words in json.loads(r.stdout):
        for w in words:
            rel = os.path.normpath(w)
            if not os.path.isabs(rel) and not rel.startswith("..") and os.path.isfile(os.path.join(ksrc, rel)):
                out.add(rel)
    return out


def probe() -> None:
    out = []
    for argv in json.load(sys.stdin):
        r = subprocess.run(argv, capture_output=True, text=True)
        if r.returncode:
            sys.stderr.write(r.stderr)
            sys.exit(1)
        text = r.stdout.replace("\\\n", " ")
        out.append(text.partition(":")[2].split())
    json.dump(out, sys.stdout)


def unescape(pat: bytes) -> bytes:
    out, i = bytearray(), 0
    while i < len(pat):
        if pat[i] == 0x5C and i + 1 < len(pat) and pat[i + 1] in b"\\/":
            out.append(pat[i + 1])
            i += 2
        else:
            out.append(pat[i])
            i += 1
    return bytes(out)


def pattern_end(rest: bytes) -> int:
    i = 1
    while i < len(rest):
        if rest[i] == 0x5C:
            i += 2
        elif rest[i] == 0x2F:
            return i + 1
        else:
            i += 1
    return -1


def address() -> None:
    with open("tags", "rb") as f:
        lines = f.read().split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    by_file: dict[bytes, list[int]] = {}
    for i, line in enumerate(lines):
        if line.startswith(b"!_TAG_"):
            continue
        parts = line.split(b"\t", 2)
        if len(parts) == 3 and parts[2].startswith(b"/^"):
            by_file.setdefault(parts[1], []).append(i)
    for name, idx in by_file.items():
        try:
            with open(name, "rb") as f:
                text = f.read().split(b"\n")
        except OSError:
            continue
        first: dict[bytes, int] = {}
        for no, t in enumerate(text, 1):
            first.setdefault(t, no)
        for i in idx:
            tag, path, rest = lines[i].split(b"\t", 2)
            k = pattern_end(rest)
            if k < 0 or not rest[k:].startswith(b';"'):
                continue
            body = rest[2:k - 1]
            slashes = len(body[:-1]) - len(body[:-1].rstrip(b"\\"))
            whole = body.endswith(b"$") and slashes % 2 == 0
            want = unescape(body[:-1] if whole else body)
            no = first.get(want) if whole else next(
                (n for n, t in enumerate(text, 1) if t.startswith(want)), None)
            if no is not None:
                lines[i] = b"\t".join((tag, path, str(no).encode() + rest[k:]))
    out, last = [], None
    for l in sorted(x + b"\n" for x in lines):
        if l != last:
            out.append(l)
        last = l
    with open("tags.kbl-tmp", "wb") as f:
        f.write(b"".join(out))
    os.replace("tags.kbl-tmp", "tags")


def listed(lists: str, compiled: set[str]) -> set[str]:
    try:
        with open(os.path.join(lists, "keys")) as f:
            keys = f.read().split()
    except FileNotFoundError:
        raise SystemExit(f"tagview.py: {lists} records no ctags pass; scripts/tags.sh ran none")
    out: set[str] = set()
    for k in keys:
        with open(os.path.join(lists, f"files.{k}")) as f:
            names = {os.path.normpath(x) for x in f.read().split("\n") if x}
        if names & compiled:
            out |= names
    return out


def masked(path: str, live: set[int]) -> tuple[bytes, bytes]:
    with open(path, "rb") as f:
        data = f.read()
    lay = ir.Layout(data.decode("utf-8", "replace"))
    lines = data.split(b"\n")
    for no in range(1, len(lines) + 1):
        g = lay.owner[no]
        if g >= 0 and g not in live:
            lines[no - 1] = b""
    return b"\n".join(lines), data


def save_json(path: str, value) -> None:
    with open(path + ".kbl-tmp", "w") as f:
        json.dump(value, f)
    os.replace(path + ".kbl-tmp", path)


def write_if_changed(path: str, content: bytes) -> bool:
    try:
        with open(path, "rb") as f:
            if f.read() == content:
                return False
    except OSError:
        pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".kbl-tmp"
    with open(tmp, "wb") as f:
        f.write(content)
    os.replace(tmp, path)
    return True


def stamp(path: str) -> list | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return [st.st_size, st.st_mtime_ns]


def load(path: str, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def cmd_build(args) -> int:
    tree = args.trees[0]
    ksrc = clangd.kernel_root(tree)
    shell = shlex.split(args.build_shell)
    units = recorded_units(ksrc)
    if not units:
        print(f"    no compile recorded in {ksrc}: nothing to read the view from")
        return 2
    jobs = args.jobs or clangd.auto_jobs(job_mb=clangd.PREPROCESS_MB)
    memo = Memo(os.path.join(args.view, "readings"),
                os.path.join(clangd.cache_dir(tree), "readings"), ir.collector_code())
    got = ir.Readings(units)

    def reads(u):
        deps = clangd.kbuild_deps(u["cmd"], symbols=True)
        return None if deps is None else [os.path.normpath(os.path.join(ksrc, x)) for x in deps]

    shown = [0]

    def progress(done, total):
        if done == total or done - shown[0] >= max(1, total // 10):
            shown[0] = done
            print(f"      {done}/{total} units preprocessed", flush=True)

    failed = ir.readings(shell, ksrc, units, memo, jobs, reads, got.take, progress)
    memo.prune()

    compiled: set[str] = set(got.files)
    unsure: set[str] = set()
    blind = 0
    for u in units:
        rel = os.path.relpath(u["file"], ksrc)
        compiled.add(rel)
        files = unit_files(ksrc, u)
        if files is None:
            blind += 1
        else:
            compiled |= files
    for u, _ in failed:
        unsure.add(os.path.relpath(u["file"], ksrc))
        unsure |= unit_files(ksrc, u) or set()
    compiled |= forced_closure(shell, ksrc, units)

    names = listed(os.path.join(args.view, "state"), compiled)
    up = os.path.join(args.view, "upper")
    before = set(load(os.path.join(args.view, STATE), []))
    want: dict[str, bytes | None] = {}
    n_mask = n_blank = n_lines = 0
    for rel in names:
        path = os.path.join(ksrc, rel)
        if rel in unsure or not os.path.isfile(path):
            want[rel] = None
        elif rel not in compiled:
            want[rel] = b""
            n_blank += 1
        elif rel in got.index:
            live = set().union(*(set(k) for k, _, _ in got.of(rel)))
            view, real = masked(path, live)
            if view != real:
                want[rel] = view
                n_mask += 1
                n_lines += sum(1 for a, b in zip(view.split(b"\n"), real.split(b"\n")) if a != b)
            else:
                want[rel] = None
        else:
            want[rel] = None

    written = removed = 0
    keep: set[str] = set()
    for rel, content in want.items():
        if content is None and rel not in before:
            continue
        if content is None:
            with open(os.path.join(ksrc, rel), "rb") as f:
                content = f.read()
        keep.add(rel)
        written += write_if_changed(os.path.join(up, rel), content)
    for rel in before - keep:
        try:
            os.unlink(os.path.join(up, rel))
            removed += 1
        except FileNotFoundError:
            pass
    save_json(os.path.join(args.view, STATE), sorted(keep))
    published = load(os.path.join(args.view, PUBLISHED), None)
    foreign = None if published is None else [
        n for n, (size, mtime) in published.items() if stamp(os.path.join(ksrc, n)) != [size, mtime]]
    if foreign is None or foreign:
        for rel in keep:
            os.utime(os.path.join(up, rel))
        print(f"    {' '.join(sorted(foreign)) if foreign else 'no index'} from the last run over "
              f"the view is in the tree: every copy counts as changed")

    print(f"    {len(units)} compiles read ({memo.hits} carried over"
          + (f", {len(failed)} refused: their files are left as they are" if failed else "")
          + (f", {blind} without a dependency list" if blind else "") + ")")
    print(f"    {len(names)} files listed, {len(compiled & names)} of them read by the build")
    print(f"    {n_mask} files with {n_lines} lines this config does not compile blanked, "
          f"{n_blank} files this build never reads emptied")
    print(f"    {written} copies written, {removed} removed, {len(keep)} in {up}")
    for u, err in failed[:10]:
        last = err.strip().splitlines()[-1] if err.strip() else "no output"
        print(f"      refused {os.path.relpath(u['file'], ksrc)}: {last}")
    return 0


def is_whiteout(path: str) -> bool:
    st = os.lstat(path)
    return (stat.S_ISCHR(st.st_mode) and st.st_rdev == 0) or os.path.basename(path).startswith(".wh.")


def publish(src: str, dst: str) -> None:
    tmp = os.path.join(os.path.dirname(dst), f".{os.path.basename(dst)}.kbl-tmp")
    if subprocess.run(["cp", "--reflink=auto", "-p", src, tmp]).returncode:
        shutil.copy2(src, tmp)
    os.replace(tmp, dst)


def cmd_stage(args) -> int:
    tree = args.trees[0]
    ksrc = clangd.kernel_root(tree)
    up = os.path.join(args.view, "upper")
    for name in sorted(OUTPUTS):
        path = os.path.join(ksrc, name)
        if os.path.islink(path) or not os.path.isfile(path) or os.path.exists(os.path.join(up, name)):
            continue
        if subprocess.run(["cp", "--reflink=auto", "--preserve=mode,timestamps", path,
                           os.path.join(up, name)]).returncode:
            shutil.copyfile(path, os.path.join(up, name))
            shutil.copystat(path, os.path.join(up, name))
    return 0


def cmd_harvest(args) -> int:
    tree = args.trees[0]
    ksrc = clangd.kernel_root(tree)
    up = os.path.join(args.view, "upper")
    view = set(load(os.path.join(args.view, STATE), []))
    started = None
    if args.keep:
        try:
            started = os.stat(os.path.join(args.view, "state", "started")).st_mtime_ns
        except OSError:
            raise SystemExit(f"tagview.py: {args.view}/state/started is missing, so nothing "
                             f"tells this run's indexes from older files; none was kept")
    published, stray = [], []
    for d, dirs, files in os.walk(up, topdown=False):
        for name in files + [x for x in dirs if os.path.islink(os.path.join(d, x))]:
            path = os.path.join(d, name)
            rel = os.path.relpath(path, up)
            if not is_whiteout(path) and rel in view:
                continue
            if (args.keep and d == up and rel in OUTPUTS and not is_whiteout(path)
                    and not os.path.islink(path) and os.path.isfile(path)
                    and os.stat(path).st_mtime_ns >= started):
                publish(path, os.path.join(ksrc, rel))
                published.append(rel)
            elif not is_whiteout(path) and (d != up or rel not in OUTPUTS):
                stray.append(rel)
            os.unlink(path)
    if args.keep:
        os.unlink(os.path.join(args.view, "state", "started"))
        record = load(os.path.join(args.view, PUBLISHED), {})
        record.update({rel: stamp(os.path.join(ksrc, rel)) for rel in published})
        save_json(os.path.join(args.view, PUBLISHED), {k: v for k, v in record.items() if v is not None})
    if published:
        print(f"    into the tree: {' '.join(sorted(published))}")
    if stray:
        print(f"    dropped {len(stray)} file(s) the run left that are no index: "
              f"{' '.join(sorted(stray)[:5])}")
    return 0


def cmd_forget(args) -> int:
    tree = args.trees[0]
    ksrc = clangd.kernel_root(tree)
    record = os.path.join(args.view, PUBLISHED)
    names = sorted(load(record, {}))
    for name in names:
        try:
            os.unlink(os.path.join(ksrc, name))
        except FileNotFoundError:
            pass
    if os.path.exists(record):
        os.unlink(record)
    if names:
        print(f"    removed what the view had indexed, to index the tree as it is: {' '.join(names)}")
    return 0


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "probe":
        probe()
        return 0
    if len(sys.argv) > 1 and sys.argv[1] == "address":
        address()
        return 0
    if len(sys.argv) > 1 and sys.argv[1] == "outputs":
        names = INDEXES[sys.argv[2]] if len(sys.argv) > 2 else sorted(OUTPUTS)
        print("\n".join(names))
        return 0
    ap = argparse.ArgumentParser(description="the tree as its build compiled it, for ctags, "
                                             "cscope and gtags")
    ap.add_argument("--tree", required=True, metavar="DIR")
    ap.add_argument("--view", required=True, metavar="DIR",
                    help="the tree's view directory, as kbuildlab chose it")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="write the view the indexers read")
    b.add_argument("--build-shell", required=True, metavar="CMD",
                   help="the command prefix that runs a program in the tree's build image, "
                        "with the kernel source as its working directory and stdin attached")
    b.add_argument("-j", "--jobs", type=int, default=0)
    b.set_defaults(func=cmd_build)
    sg = sub.add_parser("stage", help="put the tree's indexes in the layer, so a run "
                                       "updates them there rather than copying them up")
    sg.set_defaults(func=cmd_stage)
    fg = sub.add_parser("forget", help="remove the indexes a run over the view put in the tree")
    fg.set_defaults(func=cmd_forget)
    h = sub.add_parser("harvest", help="take what a run over the view left behind")
    g = h.add_mutually_exclusive_group(required=True)
    g.add_argument("--keep", action="store_true", help="move what it wrote at the top into the tree")
    g.add_argument("--drop", action="store_true", help="discard everything it wrote")
    h.set_defaults(func=cmd_harvest)
    args = ap.parse_args()
    name = clangd.register_tree(args.tree)
    if name is None:
        raise SystemExit(f"tagview.py: not a tree: {args.tree}")
    args.trees = [name]
    if args.cmd != "forget" and not os.path.isdir(os.path.join(args.view, "upper")):
        raise SystemExit(f"tagview.py: {args.view}/upper does not exist; kbuildlab creates it")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
