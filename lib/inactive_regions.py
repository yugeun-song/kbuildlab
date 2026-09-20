#!/usr/bin/env python3
"""Which #if groups of a kernel file are live, taken from the build itself.

clangd decides a header's inactive regions from whatever macros the header's
reconstructed preamble leaves defined.  The build decides them from the macros
defined at the point where a source first reaches that header.  The two differ
whenever the reaching file defined something on the way in, or the preamble
ran past the point of inclusion.

`settle` replays a source through clang's preprocessor, records the macros a
header's conditionals depend on at that point, and writes the difference as a
small forced include.  `audit` measures the result: it marks every group of a
file, preprocesses the real translation unit, and compares the groups that
survived with the regions clangd reports.  Nothing here names a header, a
macro or a tree; every input is read from the compilation database.
"""

from __future__ import annotations

import concurrent.futures as cf
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile

OPENERS = ("if", "ifdef", "ifndef")
BRANCHES = ("elif", "else", "elifdef", "elifndef")
_DIRECTIVE = re.compile(r"\s*#\s*([A-Za-z_]+)")
_IDENT = re.compile(r"[A-Za-z_]\w*")
_MARKER = re.compile(r'^# (?P<line>\d+) "(?P<path>(?:[^"\\]|\\.)*)"(?P<flags>(?: \d)*)$')
_DEFINE = re.compile(r"^#define ([A-Za-z_]\w*)")
_UNDEF = re.compile(r"^#undef ([A-Za-z_]\w*)")
_PROBE = re.compile(r"^#define KBL_GROUP_(\d+)_(\d+) 1$", re.M)
_MARK = re.compile(r"^#define KBL_GROUP_(\d+)_(\d+) 1$")


# ---------------------------------------------------------------------------
# conditional groups
# ---------------------------------------------------------------------------

def logical_lines(text: str):
    """(first physical line, last physical line, spliced text), 1-based."""
    lines = text.split("\n")
    i, n = 0, len(lines)
    while i < n:
        first = i
        buf = lines[i].rstrip("\r")
        while buf.rstrip(" \t").endswith("\\") and i + 1 < n:
            buf = buf.rstrip(" \t")[:-1] + lines[i + 1].rstrip("\r")
            i += 1
        yield first + 1, i + 1, buf
        i += 1


def strip_comments(buf: str, in_block: bool) -> tuple[str, bool]:
    """One logical line with comments removed, the way a skipping lexer reads
    it: an unterminated literal runs to the end of the line."""
    out, i, n = [], 0, len(buf)
    while i < n:
        if in_block:
            j = buf.find("*/", i)
            if j < 0:
                return "".join(out), True
            i, in_block = j + 2, False
            out.append(" ")
            continue
        c = buf[i]
        nxt = buf[i + 1] if i + 1 < n else ""
        if c == "/" and nxt == "*":
            in_block = True
            i += 2
            continue
        if c == "/" and nxt == "/":
            break
        if c in "\"'":
            j = i + 1
            while j < n and buf[j] != c:
                j += 2 if buf[j] == "\\" else 1
            out.append(buf[i:j + 1])
            i = j + 1
            continue
        out.append(c)
        i += 1
    return "".join(out), in_block


class Group:
    __slots__ = ("gid", "kind", "expr", "dir_first", "dir_last", "body_first",
                 "body_last", "parent", "chain")

    def __init__(self, gid, kind, expr, dir_first, dir_last, parent, chain):
        self.gid, self.kind, self.expr = gid, kind, expr
        self.dir_first, self.dir_last = dir_first, dir_last
        self.body_first, self.body_last = dir_last + 1, 0
        self.parent, self.chain = parent, chain


def scan(text: str) -> tuple[list[Group], set[int]]:
    """Conditional groups in source order, and the lines their directives occupy."""
    groups: list[Group] = []
    stack: list[Group] = []
    directive_lines: set[int] = set()
    in_block = False
    for first, last, buf in logical_lines(text):
        code, in_block = strip_comments(buf, in_block)
        m = _DIRECTIVE.match(code)
        if not m:
            continue
        name = m.group(1)
        if name not in OPENERS and name not in BRANCHES and name != "endif":
            continue
        directive_lines.update(range(first, last + 1))
        if name in OPENERS:
            g = Group(len(groups), name, code[m.end():].strip(), first, last,
                      stack[-1] if stack else None, len(groups))
            groups.append(g)
            stack.append(g)
        elif stack:
            closed = stack.pop()
            closed.body_last = first - 1
            if name in BRANCHES:
                g = Group(len(groups), name, code[m.end():].strip(), first, last,
                          closed.parent, closed.chain)
                groups.append(g)
                stack.append(g)
    total = text.count("\n") + 1
    for g in stack:
        g.body_last = total
    return groups, directive_lines


_NOT_DEFINED = re.compile(r"!\s*defined\s*\(?\s*([A-Za-z_]\w*)\s*\)?$")


def guard_of(text: str) -> str | None:
    """The macro a header tests first and then defines: its include guard."""
    groups, _ = scan(text)
    if not groups or groups[0].parent is not None:
        return None
    g = groups[0]
    if g.kind == "ifndef":
        m = _IDENT.match(g.expr)
    elif g.kind == "if":
        m = _NOT_DEFINED.match(g.expr)
    else:
        return None
    if not m:
        return None
    name = m.group(m.lastindex or 0)
    body = "\n".join(text.split("\n")[g.body_first - 1:g.body_last])
    return name if re.search(rf"^\s*#\s*define\s+{re.escape(name)}\b", body, re.M) else None


def tested_names(groups: list[Group]) -> set[str]:
    names: set[str] = set()
    for g in groups:
        names.update(_IDENT.findall(g.expr))
    names.discard("defined")
    return names


def instrument(text: str, fid: int, groups: list[Group]) -> str:
    """The same file with a uniquely named macro defined inside every group."""
    after: dict[int, list[str]] = {}
    for g in groups:
        after.setdefault(g.dir_last, []).append(f"#define KBL_GROUP_{fid}_{g.gid} 1")
    out = [f"#define KBL_GROUP_{fid}_{len(groups)} 1"]
    for no, line in enumerate(text.split("\n"), 1):
        out.append(line)
        out.extend(after.get(no, ()))
    return "\n".join(out)


def original_lines(text: str, groups: list[Group]) -> list[int]:
    """Line of `text` that each line of instrument(text, ...) came from."""
    added = {}
    for g in groups:
        added[g.dir_last] = added.get(g.dir_last, 0) + 1
    out = [1]
    for no in range(1, text.count("\n") + 2):
        out += [no] * (1 + added.get(no, 0))
    return out


def several_passes(groups: list[Group], live: set[int]) -> bool:
    """Two branches of one #if chain both live: the file was read more than
    once under different macros, so no single reading matches the build."""
    chains = [g.chain for g in groups if g.gid in live]
    return len(chains) != len(set(chains))


def dead_lines(groups: list[Group], live: set[int], directive_lines: set[int]) -> set[int]:
    dead: set[int] = set()
    for g in groups:
        if g.gid not in live:
            dead.update(range(g.body_first, g.body_last + 1))
    return dead - directive_lines


# ---------------------------------------------------------------------------
# the command clang is actually given
# ---------------------------------------------------------------------------

class View:
    """How this tree's GCC command lines reach clang: the target, the flags
    clang refuses, and the system include directories that exist here."""

    def __init__(self, ksrc: str, triple: str, bad: set[str],
                 subs: dict[str, tuple[str, set[str]]], isystem: list[str]):
        self.ksrc, self.main, self.subs, self.isystem = ksrc, (triple, bad), subs, isystem

    def flags(self, args: list[str], path: str) -> list[str]:
        """clang argv for an entry's arguments, input file left out."""
        rel = os.path.relpath(path, self.ksrc)
        triple, bad = self.main
        for prefix, sub in self.subs.items():
            if rel.startswith(prefix):
                triple, bad = sub
        body = args[1:-3] if args[-3:-1] == ["-x", "c-header"] else args[1:-1]
        out, i = ["clang", f"--target={triple}", "-w"], 0
        while i < len(body):
            a = body[i]
            if a in ("-o", "-MT", "-MF"):
                i += 2
            elif a == "-isystem":
                if os.path.isdir(body[i + 1]):
                    out += [a, body[i + 1]]
                i += 2
            elif a.startswith("-isystem"):
                if os.path.isdir(a[len("-isystem"):]):
                    out.append(a)
                i += 1
            elif a == "-c" or a.startswith("-Wp,-M") or a in bad:
                i += 1
            else:
                out.append(a)
                i += 1
        return out + self.isystem


def _vfs(workdir: str, mapping: dict[str, str]) -> str:
    fd, path = tempfile.mkstemp(suffix=".yaml", dir=workdir)
    with os.fdopen(fd, "w") as f:
        json.dump({"version": 0, "case-sensitive": "true", "use-external-names": "false",
                   "roots": [{"type": "file", "name": k, "external-contents": v}
                             for k, v in mapping.items()]}, f)
    return path


# ---------------------------------------------------------------------------
# settle: make a header's conditionals see what the build saw
# ---------------------------------------------------------------------------

def _closure(names: set[str], state: dict[str, str]) -> set[str]:
    seen, todo = set(), list(names)
    while todo:
        n = todo.pop()
        if n in seen:
            continue
        seen.add(n)
        body = state.get(n)
        if body:
            todo.extend(x for x in _IDENT.findall(body) if x not in seen)
    return seen


def _trace(job):
    """Macro state on entry to each wanted header while one source is read.

    Every wanted header is read through a marked copy, so the stream says when
    it is entered and which of its groups survive.  A header read more than
    once -- trace/define_trace.h, once per event header -- is taken at the
    entry that compiles the most of it.

    wanted: marker id -> (key, names its conditionals test, group count,
    lines per group, has an include guard).
    Returns {key: (state, later, origin)}: `state` maps each name those
    conditionals depend on to its definition or None; `later` maps a name to
    the chain of nested files that defines it before the header is left again;
    `origin` says where each definition was read.
    """
    argv, source, cwd, wanted, vfs, renumber = job
    proc = subprocess.Popen(argv + ["-ivfsoverlay", vfs, "-E", "-dD", source], cwd=cwd,
                            text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            errors="replace")
    state: dict[str, str] = {}
    where: dict[str, str] = {}
    entries: dict[str, list[dict]] = {}
    # A header can reach itself again before it is left -- linux/linkage.h
    # does, through asm/linkage.h -- so its open entries form a stack.
    open_at: dict[str, list[tuple[int, dict]]] = {}
    pending = {key for key, *_ in wanted.values()}
    guarded = {w[0]: w[4] for w in wanted.values()}
    stack: list[str] = []
    here, path, lineno = "", "", 0
    assert proc.stdout is not None
    for line in proc.stdout:
        lineno += 1
        if not line.startswith("#"):
            continue
        m = _MARK.match(line)
        if m:
            hit = wanted.get(int(m.group(1)))
            if hit is None:
                continue
            key, names, count = hit[:3]
            if int(m.group(2)) != count:
                if open_at.get(key):
                    open_at[key][-1][1]["live"].add(int(m.group(2)))
                continue
            needed = _closure(names, state)
            entry = {"state": {n: state.get(n) for n in needed}, "later": {}, "live": set(),
                     "origin": {n: where[n] for n in needed if n in where}}
            entries.setdefault(key, []).append(entry)
            open_at.setdefault(key, []).append((len(stack), entry))
            continue
        m = _DEFINE.match(line)
        if m:
            name = m.group(1)
            state[name] = line.rstrip("\n")
            shift = renumber.get(path)
            where[name] = f"{here}:{shift[lineno - 2] if shift else lineno - 1}"
            for opened in open_at.values():
                for depth, entry in opened:
                    if name in entry["state"] and len(stack) > depth:
                        entry["later"].setdefault(name, stack[depth:])
            continue
        m = _UNDEF.match(line)
        if m:
            state.pop(m.group(1), None)
            continue
        m = _MARKER.match(line.rstrip("\n"))
        if not m:
            continue
        lineno = int(m["line"])
        path = os.path.normpath(os.path.join(cwd, m["path"]))
        here = os.path.relpath(path, cwd) if path.startswith(cwd + os.sep) else m["path"]
        if " 2" in m["flags"]:
            while stack and stack[-1] != path:
                stack.pop()
            for key, opened in open_at.items():
                while opened and len(stack) < opened[-1][0]:
                    opened.pop()
                if not opened and key in entries and guarded[key]:
                    pending.discard(key)
            if not pending:
                break
        elif " 1" in m["flags"]:
            stack.append(path)
    proc.kill()
    proc.wait()
    best: dict[str, tuple[dict, dict, dict]] = {}
    for fid, (key, _, _, lines, _) in wanted.items():
        if key in entries and key not in best:
            e = max(entries[key], key=lambda x: sum(lines[g] for g in x["live"]))
            best[key] = (e["state"], e["later"], e["origin"])
    return best


def _preamble(job):
    """Definitions the header's own preamble leaves for the names that matter."""
    argv, header, cwd, names, workdir, empty = job
    vfs = _vfs(workdir, {header: empty})
    r = subprocess.run(argv + ["-ivfsoverlay", vfs, "-E", "-dM", "-x", "c", os.devnull],
                       cwd=cwd, capture_output=True, text=True, errors="replace")
    os.unlink(vfs)
    have: dict[str, str] = {}
    for line in r.stdout.splitlines():
        m = _DEFINE.match(line)
        if m and m.group(1) in names:
            have[m.group(1)] = line
    return have


class Memo:
    """Results kept under a digest of everything they were computed from.

    A result is reused only while the command, this module and every file the
    build recorded as read are unchanged, so a rebuild that touched forty files
    repeats the work for what includes those forty.  What a run did not ask
    for is dropped when it ends.
    """

    def __init__(self, directory: str):
        self.dir = directory
        os.makedirs(directory, exist_ok=True)
        with open(os.path.abspath(__file__), "rb") as f:
            self.code = hashlib.sha1(f.read()).hexdigest()
        self.used: set[str] = set()
        self.hits = 0

    @staticmethod
    def stamp(paths) -> list:
        out = []
        for p in sorted(paths):
            try:
                st = os.stat(p)
                out.append([p, st.st_size, st.st_mtime_ns])
            except OSError:
                out.append([p, None, None])
        return out

    def key(self, *parts) -> str:
        blob = json.dumps([self.code, *parts], sort_keys=True, default=sorted)
        return hashlib.sha1(blob.encode()).hexdigest()

    def get(self, key: str):
        self.used.add(key)
        try:
            with open(os.path.join(self.dir, key)) as f:
                value = json.load(f)
        except (OSError, ValueError):
            return None
        self.hits += 1
        return value

    def put(self, key: str, value) -> None:
        tmp = os.path.join(self.dir, key + ".tmp")
        with open(tmp, "w") as f:
            json.dump(value, f, default=sorted)
        os.replace(tmp, os.path.join(self.dir, key))

    def prune(self) -> None:
        for name in os.listdir(self.dir):
            if name not in self.used:
                os.unlink(os.path.join(self.dir, name))


def settle(view: View, cache: str, sources: dict[str, dict], headers: list[dict],
           chosen: dict[str, str], standins: dict[str, str], floor: str | None,
           entry_args, jobs: int, verbose: bool, reads=None) -> dict[str, int]:
    """Give each header entry the macro state its context source has on entry.

    chosen maps a header to the source whose command it borrowed; standins maps
    an overlay copy to the tree header it shadows, since the build's include
    path reaches the copy.  Entries are amended in place.

    reads(source) names the files the build recorded that source as reading;
    with it, a source none of whose inputs changed is not preprocessed again.
    """
    ksrc = view.ksrc
    state_dir = os.path.join(cache, "state")
    shutil.rmtree(state_dir, ignore_errors=True)
    shadow = {tree: copy for copy, tree in standins.items()}

    workdir = tempfile.mkdtemp(prefix="settle.", dir=cache)
    memo = Memo(os.path.join(cache, "memo"))
    try:
        done = _settle(view, state_dir, workdir, sources, headers, chosen, shadow, floor,
                       entry_args, jobs, verbose, memo, reads or (lambda source: None))
        memo.prune()
        return done
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _settle(view, state_dir, workdir, sources, headers, chosen, shadow, floor,
            entry_args, jobs, verbose, memo, reads):
    ksrc = view.ksrc
    per_source: dict[str, dict] = {}
    texts_of: dict[int, list[tuple[str, str]]] = {}
    marked: dict[str, dict[str, str]] = {}
    renumber: dict[str, dict[str, list[int]]] = {}
    traced = 0
    for fid, e in enumerate(headers):
        rel = os.path.relpath(e["file"], ksrc)
        src = chosen.get(rel)
        if src is None or src not in sources:
            continue
        paths = [e["file"]] + ([shadow[e["file"]]] if e["file"] in shadow else [])
        texts = []
        for p in paths:
            with open(p, encoding="utf-8", errors="replace") as f:
                texts.append(f.read())
        groups, _ = scan(texts[0])
        if not groups:
            continue
        traced += 1
        lines = {g.gid: g.body_last - g.body_first + 1 for g in groups}
        per_source.setdefault(src, {})[fid] = (rel, tested_names(groups), len(groups), lines,
                                               guard_of(texts[0]) is not None)
        texts_of[fid] = list(zip(paths, texts))

    stamps: dict[str, list | None] = {}
    target: dict[str, dict] = {}
    jobs_a, keys_a = [], []
    for src, wanted in per_source.items():
        s = sources[src]
        argv = view.flags(entry_args(s), s["file"])
        read = reads(src)
        stamps[src] = None if read is None else Memo.stamp(
            set(read) | {s["file"]} | {p for fid in wanted for p, _ in texts_of[fid]})
        key = memo.key("trace", argv, sorted(w[0] for w in wanted.values()), stamps[src])
        found = memo.get(key) if stamps[src] is not None else None
        if found is not None:
            target.update(found)
            continue
        for fid in wanted:
            for n, (p, text) in enumerate(texts_of[fid]):
                own = scan(text)[0]
                if len(own) != wanted[fid][2]:
                    continue
                copy = os.path.join(workdir, f"{fid}.{n}")
                with open(copy, "w") as f:
                    f.write(instrument(text, fid, own))
                marked.setdefault(src, {})[os.path.normpath(p)] = copy
                renumber.setdefault(src, {})[os.path.normpath(p)] = original_lines(text, own)
        jobs_a.append((argv, s["file"], s["directory"], wanted,
                       _vfs(workdir, marked[src]), renumber[src]))
        keys_a.append(key if stamps[src] is not None else None)
    with cf.ProcessPoolExecutor(jobs) as ex:
        for key, found in zip(keys_a, ex.map(_trace, jobs_a, chunksize=4)):
            target.update(found)
            if key:
                memo.put(key, found)
    retraced = len(jobs_a)

    written = 0
    guards: dict[str, str | None] = {}
    empty = os.path.join(workdir, "empty.h")
    open(empty, "w").close()
    jobs_b, keys_b = [], []
    by_rel = {os.path.relpath(e["file"], ksrc): e for e in headers}
    have_of: dict[str, dict] = {}
    for rel, snap in target.items():
        e = by_rel[rel]
        argv = view.flags(entry_args(e), e["file"])
        if floor:
            argv += ["-include", floor]
        stamp = stamps[chosen[rel]]
        key = memo.key("preamble", argv, sorted(snap[0]), stamp)
        have = memo.get(key) if stamp is not None else None
        if have is not None:
            have_of[rel] = have
            continue
        jobs_b.append((argv, e["file"], e["directory"], set(snap[0]), workdir, empty))
        keys_b.append((rel, key if stamp is not None else None))
    with cf.ProcessPoolExecutor(jobs) as ex:
        for (rel, key), have in zip(keys_b, ex.map(_preamble, jobs_b, chunksize=8)):
            have_of[rel] = have
            if key:
                memo.put(key, have)

    for rel, have in have_of.items():
        want_state, later, origin = target[rel]
        fix, reopen = [], []
        for name in sorted(want_state):
            want = want_state[name]
            if want == have.get(name):
                continue
            fix.append(f"#undef {name}")
            if want:
                fix.append(f"{want.rstrip()} /* {origin[name]} */")
            else:
                reopen += later.get(name, [])
        # A name the header's own includes define later must be defined
        # there again, so the files on the way to it are made enterable.
        for nested in dict.fromkeys(reopen):
            guard = guards.get(nested)
            if guard is None and nested not in guards:
                try:
                    with open(nested, encoding="utf-8", errors="replace") as f:
                        guard = guard_of(f.read())
                except OSError:
                    guard = None
                guards[nested] = guard
            if guard:
                fix.append(f"#undef {guard}")
        if not fix:
            continue
        path = os.path.join(state_dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(f"/* Macro state {chosen[rel]} has on entering this header. */\n")
            if floor:
                f.write(f"#include <{floor}>\n")
            f.write("\n".join(fix) + "\n")
        e = by_rel[rel]
        args = entry_args(e)
        e.pop("arguments", None)
        e["command"] = shlex.join(args[:-3] + ["-include", path] + args[-3:])
        written += 1
    if verbose:
        print(f"    {len(target)} headers traced through {len(per_source)} sources "
              f"({retraced} preprocessed, the rest unchanged since last time); "
              f"{written} needed their macro state corrected, "
              f"{traced - len(target)} are never reached by their source")
    return {"traced": len(target), "corrected": written, "unreached": traced - len(target)}


# ---------------------------------------------------------------------------
# audit: compare clangd's inactive regions with the build's
# ---------------------------------------------------------------------------

def live_groups(view: View, source: dict, files: dict[str, int], entry_args,
                workdir: str) -> dict[int, set[int]]:
    """Groups of `files` (path -> id) that survive preprocessing `source`."""
    d = tempfile.mkdtemp(dir=workdir)
    try:
        mapping = {}
        for path, fid in files.items():
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
            groups, _ = scan(text)
            marked = os.path.join(d, f"{fid}.{len(mapping)}")
            with open(marked, "w") as f:
                f.write(instrument(text, fid, groups))
            mapping[path] = marked
        argv = view.flags(entry_args(source), source["file"])
        r = subprocess.run(argv + ["-ivfsoverlay", _vfs(d, mapping), "-E", "-dM", source["file"]],
                           cwd=source["directory"], capture_output=True, text=True,
                           errors="replace")
    finally:
        shutil.rmtree(d, ignore_errors=True)
    live: dict[int, set[int]] = {}
    for fid, gid in _PROBE.findall(r.stdout):
        live.setdefault(int(fid), set()).add(int(gid))
    return live
