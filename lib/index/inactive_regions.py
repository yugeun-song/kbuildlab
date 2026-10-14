#!/usr/bin/env python3
"""Which #if groups of a kernel file are live, taken from the build itself.

clangd decides a header's inactive regions from whatever macros the header's
reconstructed preamble leaves defined.  The build decides them from the macros
defined at the point where a source first reaches that header.  The two differ
whenever the reaching file defined something on the way in, or the preamble
ran past the point of inclusion.

`settle` replays a source through clang's preprocessor, records the macros a
header's conditionals depend on at that point, and writes the difference as a
small forced include.  Nothing here names a header, a macro or a tree; every
input is read from the compilation database.

clangd is clang, and the build compiler is not: `__clang__`, `__GNUC__` and
the `__has_*` operators answer differently.  `identity` asks both compilers
about exactly the names the tree's conditionals depend on, and writes the
build compiler's answers as a forced include wherever clang can follow them.

`collect` runs inside the build image: every unit goes through the build
compiler with -fdirectives-only, which copies each surviving line verbatim, so
the groups a unit compiled are read off the output.  `readings` drives it from
the host and keeps each unit's result until its inputs change.
"""

from __future__ import annotations

import array
import concurrent.futures as cf
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import zlib

OPENERS = ("if", "ifdef", "ifndef")
BRANCHES = ("elif", "else", "elifdef", "elifndef")
_DIRECTIVE = re.compile(r"\s*#\s*([A-Za-z_]+)")
_IDENT = re.compile(r"[A-Za-z_]\w*")
_MARKER = re.compile(r'^# (?P<line>\d+) "(?P<path>(?:[^"\\]|\\.)*)"(?P<flags>(?: \d)*)$')
_DEFINE = re.compile(r"^#define ([A-Za-z_]\w*)")
_UNDEF = re.compile(r"^#undef ([A-Za-z_]\w*)")
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


_OUTPUT_DIRECTIVES = ("define", "undef", "pragma")


class Layout:
    """A file's groups, the innermost group of each line, and the lines a
    preprocessor with -fdirectives-only copies to its output when compiled."""

    def __init__(self, text: str):
        self.groups, _ = scan(text)
        n = text.count("\n") + 1
        self.owner = [-1] * (n + 2)
        for g in self.groups:
            for no in range(g.body_first, min(g.body_last, n) + 1):
                self.owner[no] = g.gid
        self.parent = [g.parent.gid if g.parent is not None else -1 for g in self.groups]
        self.visible: set[int] = set()
        self.renumbers = False
        in_block = tail = False
        for first, last, buf in logical_lines(text):
            began_in_comment = in_block
            code, in_block = strip_comments(buf, in_block)
            if began_in_comment and tail:
                tail = in_block
                continue
            m = None if began_in_comment else _DIRECTIVE.match(code)
            if m:
                if m.group(1) in _OUTPUT_DIRECTIVES:
                    self.visible.add(first)
                elif m.group(1) == "line":
                    self.renumbers = True
                tail = in_block
                continue
            tail = False
            if buf.strip():
                self.visible.add(first)

    def live(self, emitted) -> set[int]:
        out: set[int] = set()
        for no in emitted:
            g = self.owner[no] if 0 <= no < len(self.owner) else -1
            while g >= 0 and g not in out:
                out.add(g)
                g = self.parent[g]
        return out

    def lit(self, live) -> set[int]:
        return {no for no in self.visible if self.owner[no] < 0 or self.owner[no] in live}


def layout_of(path: str, cache: dict) -> Layout | None:
    if path not in cache:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                cache[path] = Layout(f.read())
        except OSError:
            cache[path] = None
    return cache[path]


# ---------------------------------------------------------------------------
# the command clang is actually given
# ---------------------------------------------------------------------------

class View:
    """How an entry of the database reaches clang: its command already names
    the target and holds only flags clang takes, so the view drops what a
    preprocessing run must not do -- write objects or dependency files -- and
    the input."""

    def __init__(self, ksrc: str):
        self.ksrc = ksrc

    def flags(self, args: list[str], path: str) -> list[str]:
        """clang argv for an entry's arguments, input file left out."""
        body = args[1:-3] if len(args) > 3 and args[-3] == "-x" else args[1:-1]
        out, i = ["clang", "-w"], 0
        while i < len(body):
            a = body[i]
            if a in ("-o", "-MT", "-MF", "-MQ"):
                i += 2
            elif a in ("-c", "-MD", "-MMD") or a.startswith("-Wp,-M"):
                i += 1
            else:
                out.append(a)
                i += 1
        return out


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
    whole = False
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
                whole = True
                break
        elif " 1" in m["flags"]:
            stack.append(path)
    if whole:
        proc.kill()
    whole = proc.wait() == 0 or whole
    best: dict[str, tuple[dict, dict, dict, int]] = {}
    for fid, (key, _, _, lines, _) in wanted.items():
        if key in entries and key not in best:
            e = max(entries[key], key=lambda x: sum(lines[g] for g in x["live"]))
            best[key] = (e["state"], e["later"], e["origin"], sum(lines[g] for g in e["live"]))
    return best, whole


def _preamble(job):
    """Definitions the header's own preamble leaves for the names that matter.

    Replayed from -dD rather than read from -dM: with the header emptied, clang
    in -dM mode can stop lexing part way through the preamble without an error.
    """
    argv, header, cwd, names, workdir, empty = job
    vfs = _vfs(workdir, {header: empty})
    proc = subprocess.Popen(argv + ["-ivfsoverlay", vfs, "-E", "-dD", "-x", "c", os.devnull],
                            cwd=cwd, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, errors="replace")
    have: dict[str, str] = {}
    assert proc.stdout is not None
    for line in proc.stdout:
        if not line.startswith("#"):
            continue
        m = _DEFINE.match(line)
        if m:
            if m.group(1) in names:
                have[m.group(1)] = line.rstrip("\n")
            continue
        m = _UNDEF.match(line)
        if m:
            have.pop(m.group(1), None)
    rc = proc.wait()
    os.unlink(vfs)
    return have, rc == 0


class Memo:
    """Results kept under a digest of everything they were computed from.

    A result is reused only while the command, the code that computed it and
    every file the build recorded as read are unchanged, so a rebuild that
    touched forty files repeats the work for what includes those forty.  What a
    run did not ask for is dropped when it ends.  The code is this module
    unless the caller names the source that computed the results.
    """

    def __init__(self, directory: str, code: str | None = None):
        self.dir = directory
        os.makedirs(directory, exist_ok=True)
        if code is None:
            with open(os.path.abspath(__file__), encoding="utf-8") as f:
                code = f.read()
        self.code = hashlib.sha1(code.encode()).hexdigest()
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

    def get_packed(self, key: str):
        self.used.add(key)
        try:
            with open(os.path.join(self.dir, key), "rb") as f:
                value = json.loads(zlib.decompress(f.read()))
        except (OSError, ValueError, zlib.error):
            return None
        self.hits += 1
        return value

    def put_packed(self, key: str, value) -> None:
        tmp = os.path.join(self.dir, key + ".tmp")
        with open(tmp, "wb") as f:
            f.write(zlib.compress(json.dumps(value, separators=(",", ":")).encode()))
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

    workdir = os.path.join(cache, "settle")
    shutil.rmtree(workdir, ignore_errors=True)
    os.makedirs(workdir)
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
    unfinished = 0
    with cf.ProcessPoolExecutor(jobs) as ex:
        for key, (found, whole) in zip(keys_a, ex.map(_trace, jobs_a, chunksize=4)):
            target.update(found)
            if key and whole:
                memo.put(key, found)
            unfinished += not whole
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
        for (rel, key), (have, whole) in zip(keys_b, ex.map(_preamble, jobs_b, chunksize=8)):
            have_of[rel] = have
            if key and whole:
                memo.put(key, have)
            unfinished += not whole

    for rel, have in have_of.items():
        want_state, later, origin, _ = target[rel]
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
              f"{traced - len(target)} are never reached by their source"
              + (f"; {unfinished} clang run(s) did not finish and are not kept" if unfinished else ""))
    empty = {os.path.relpath(headers[fid]["file"], ksrc): src
             for src, wanted in per_source.items() for fid, w in wanted.items()
             if w[0] not in target or not target[w[0]][3]}
    return {"traced": len(target), "corrected": written, "unreached": traced - len(target),
            "empty": empty}


# ---------------------------------------------------------------------------
# collect: what the build compiler compiled, unit by unit
# ---------------------------------------------------------------------------

def unit_argv(command: str) -> list[str]:
    """A unit's build command, turned into one that prints what it compiled."""
    args = shlex.split(command)
    out, i = [], 0
    while i < len(args) - 1:
        a = args[i]
        if a in ("-o", "-MF", "-MT", "-MQ"):
            i += 2
        elif a in ("-c", "-MD", "-MMD") or a.startswith("-Wp,-M"):
            i += 1
        else:
            out.append(a)
            i += 1
    return out + ["-E", "-fdirectives-only", "-o", "-", args[-1]]


_LAYOUTS: dict = {}


def read_unit(job):
    """Per file the unit entered: the live groups of each distinct pass."""
    key, argv, cwd = job
    root = cwd.rstrip("/") + "/"
    proc = subprocess.Popen(argv, cwd=cwd, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, errors="replace")
    passes: dict[str, list[set[int]]] = {}
    stack: list[tuple[str, set[int]]] = []
    renamed: set[str] = set()
    main = os.path.normpath(os.path.join(cwd, argv[-1]))
    line = 0
    assert proc.stdout is not None and proc.stderr is not None
    for raw in proc.stdout:
        head = raw.lstrip(" \t")
        m = _MARKER.match(head.rstrip("\n")) if head.startswith("# ") else None
        if m:
            path = os.path.normpath(os.path.join(cwd, re.sub(r"\\(.)", r"\1", m["path"])))
            line = int(m["line"])
            if " 1" in m["flags"]:
                stack.append((path, set()))
                passes.setdefault(path, []).append(stack[-1][1])
            elif " 2" in m["flags"]:
                if stack:
                    stack.pop()
                while stack and stack[-1][0] != path:
                    stack.pop()
                if stack:
                    stack[-1][1].add(line - 1)
            elif not stack or stack[-1][0] != path:
                if len(stack) > 1 or (stack and path != main and not m["path"].startswith("<")):
                    renamed.add(path)
                stack[-1:] = [(path, set())]
                passes.setdefault(path, []).append(stack[-1][1])
            continue
        if stack and raw.strip():
            stack[-1][1].add(line)
        line += 1
    err = proc.stderr.read()
    rc = proc.wait()
    files, outside = {}, []
    for path, emitted in passes.items():
        if not path.startswith(root):
            outside.append(path)
            continue
        lay = layout_of(path, _LAYOUTS)
        if lay is None or not lay.groups or lay.renumbers or path in renamed:
            continue
        # A pass that emitted lines is a reading even when none lies in a group:
        # every group of the file was dead.  One that emitted none re-entered a
        # guarded header and read nothing.
        seen = sorted({tuple(sorted(lay.live(e))) for e in emitted if e})
        if seen:
            files[os.path.relpath(path, cwd)] = [list(r) for r in seen]
    return key, rc, err[-2000:] if rc else "", {"files": files, "outside": sorted(outside)}


def collector_code() -> str:
    """The source that decides a unit's readings, which is all a kept reading
    has to be recomputed for."""
    import inspect
    return "".join(inspect.getsource(x) for x in (
        logical_lines, strip_comments, Group, scan, Layout, layout_of, unit_argv, read_unit))


def collect() -> None:
    """Inside the build image: a spec on stdin, one JSON line per unit out."""
    spec = json.load(sys.stdin)
    jobs = [(key, argv, spec["cwd"]) for key, argv in spec["units"]]
    with cf.ProcessPoolExecutor(spec["jobs"]) as ex:
        for key, rc, err, record in ex.map(read_unit, jobs):
            sys.stdout.write(json.dumps({"key": key, "rc": rc, "err": err, "record": record}) + "\n")
            sys.stdout.flush()


def readings(shell: list[str], ksrc: str, units: list[dict], memo: Memo, jobs: int,
             reads, take, progress=None) -> list:
    """Hand take(n, record) each unit's per-file readings and the files it read
    outside the tree; return the units the build compiler refused.

    reads(unit) names what kbuild recorded the unit as reading; a unit none of
    whose inputs changed is not preprocessed again.
    """
    todo, keys = [], {}
    for n, u in enumerate(units):
        argv = unit_argv(u["command"])
        read = reads(u)
        key = None if read is None else memo.key(
            "reading", argv, Memo.stamp(set(read) | {u["file"]}))
        found = memo.get_packed(key) if key else None
        if found is not None:
            take(n, found)
        else:
            todo.append([n, argv])
            keys[n] = key
    failed = []
    if not todo:
        return failed
    proc = subprocess.Popen(shell + ["python3", os.path.abspath(__file__), "collect"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    assert proc.stdin is not None and proc.stdout is not None
    try:
        proc.stdin.write(json.dumps({"cwd": ksrc, "jobs": jobs, "units": todo}))
        proc.stdin.close()
    except BrokenPipeError:
        proc.wait()
        raise SystemExit(f"the build image did not take the units: {shlex.join(shell)}")
    done = 0
    for line in proc.stdout:
        row = json.loads(line)
        n = row["key"]
        done += 1
        if row["rc"]:
            failed.append((units[n], row["err"]))
        else:
            take(n, row["record"])
            if keys[n]:
                memo.put_packed(keys[n], row["record"])
        if progress:
            progress(done, len(todo))
    if proc.wait():
        raise SystemExit(f"the build image could not preprocess: {shlex.join(shell)}")
    return failed


def best_reading(lits: list[set[int]], counts: list[int]) -> int:
    """The reading that shows the most of what the units compile: each line
    counts once per unit that compiles it.  A reading no counted unit has
    competes only when none has."""
    weight: dict[int, int] = {}
    for lit, c in zip(lits, counts):
        for no in lit:
            weight[no] = weight.get(no, 0) + c
    pool = [i for i, c in enumerate(counts) if c] or range(len(lits))
    return max(pool, key=lambda i: (sum(weight[no] for no in lits[i]), counts[i], -i))


class Readings:
    """Every distinct reading of each file over all units, how many units read
    it that way, and which units did."""

    def __init__(self, units: list[dict]):
        self.units = units
        self.index: dict[str, int] = {}
        self.files: list[str] = []
        self.seen: list[dict[tuple, int]] = []
        self.count: list[list[int]] = []
        self.first: list[list[int]] = []
        self.file_of = [array.array("I") for _ in units]
        self.reading_of = [array.array("I") for _ in units]
        self.outside: list[list[str]] = [[] for _ in units]
        self._keys: dict[int, list[tuple]] = {}

    def take(self, n: int, record: dict) -> None:
        self.outside[n] = record["outside"]
        for rel, found in record["files"].items():
            fi = self.index.get(rel)
            if fi is None:
                fi = self.index[rel] = len(self.files)
                self.files.append(rel)
                self.seen.append({})
                self.count.append([])
                self.first.append([])
            for r in found:
                key = tuple(r)
                rid = self.seen[fi].get(key)
                if rid is None:
                    rid = self.seen[fi][key] = len(self.count[fi])
                    self.count[fi].append(0)
                    self.first[fi].append(n)
                self.count[fi][rid] += 1
                self.file_of[n].append(fi)
                self.reading_of[n].append(rid)

    def of(self, rel: str) -> list[tuple[tuple, int, int]]:
        """(live groups, units reading it so, a unit that did) per reading."""
        fi = self.index[rel]
        return [(key, self.count[fi][rid], self.first[fi][rid])
                for key, rid in self.seen[fi].items()]

    def best(self, rel: str, lay: Layout) -> int:
        fi = self.index[rel]
        return best_reading([lay.lit(set(key)) for key in self.seen[fi]], self.count[fi])

    def live(self, n: int) -> dict[str, set[int]]:
        """Per file unit n entered, the groups any of its passes compiled."""
        out: dict[str, set[int]] = {}
        for fi, rid in zip(self.file_of[n], self.reading_of[n]):
            if fi not in self._keys:
                self._keys[fi] = list(self.seen[fi])
            out.setdefault(self.files[fi], set()).update(self._keys[fi][rid])
        return out

    def discount(self, units: set[int]) -> None:
        """Stop counting how these units read a file, except a file only they read."""
        minus: dict[int, dict[int, int]] = {}
        for n in units:
            for fi, rid in zip(self.file_of[n], self.reading_of[n]):
                m = minus.setdefault(fi, {})
                m[rid] = m.get(rid, 0) + 1
        for fi, m in minus.items():
            left = [c - m.get(rid, 0) for rid, c in enumerate(self.count[fi])]
            if any(left):
                self.count[fi] = left

    def compilers(self, best: dict[int, int]) -> list[bytearray]:
        """Per unit, a bit per file set where the unit reads it the best way."""
        bits = [bytearray((len(self.files) + 7) // 8) for _ in self.units]
        for n in range(len(self.units)):
            for fi, rid in zip(self.file_of[n], self.reading_of[n]):
                if best.get(fi) == rid:
                    bits[n][fi >> 3] |= 1 << (fi & 7)
        return bits


# ---------------------------------------------------------------------------
# identity: the build compiler's answers where clang's differ
# ---------------------------------------------------------------------------

_FEATURE_TEST = re.compile(r"\b(__has_[a-z_]+)\s*\(\s*([^()]*?)\s*\)")
_DEFINITION = re.compile(r"[ \t]*#[ \t]*define[ \t]+([A-Za-z_]\w*)(?:\(([^)]*)\))?(.*)")
_DEFINED = re.compile(r"\bdefined\b\s*(?:\(\s*([A-Za-z_]\w*)\s*\)|([A-Za-z_]\w*))")
_INTEGER = re.compile(r"(-?)(0[xX][0-9a-fA-F]+|[0-9]+)[uUlL]*")


def _split(expr: str) -> tuple[set[str], set[str]]:
    """Names an expression asks whether they are defined, and those it expands."""
    return ({a or b for a, b in _DEFINED.findall(expr)},
            set(_IDENT.findall(_DEFINED.sub(" ", expr))))


def questions(paths) -> tuple[set[str], dict[str, set[str]]]:
    """Names these files' conditionals depend on, and the arguments each
    feature-test operator is asked about.  A name a condition asks whether it
    is defined depends on nothing more; one it expands depends on the names in
    its definition too.  An operator reached through a macro parameter cannot
    be answered from a table and is left out."""
    checked: set[str] = set()
    expanded: set[str] = set()
    bodies: dict[str, tuple[set[str], set[str]]] = {}
    asked: dict[str, set[str]] = {}
    wrapped: set[str] = set()
    for path in paths:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        for g in scan(text)[0]:
            if g.kind in ("ifdef", "ifndef", "elifdef", "elifndef"):
                checked.update(_IDENT.findall(g.expr)[:1])
            else:
                d, e = _split(g.expr)
                checked |= d
                expanded |= e
            for op, arg in _FEATURE_TEST.findall(g.expr):
                asked.setdefault(op, set()).add(arg)
        in_block = False
        for _, _, buf in logical_lines(text):
            code, in_block = strip_comments(buf, in_block)
            m = _DEFINITION.match(code)
            if not m:
                continue
            params = {p.strip() for p in (m.group(2) or "").split(",")}
            d, e = _split(m.group(3))
            mine = bodies.setdefault(m.group(1), (set(), set()))
            mine[0].update(d - params)
            mine[1].update(e - params)
            for op, arg in _FEATURE_TEST.findall(m.group(3)):
                if arg in params:
                    wrapped.add(op)
                else:
                    asked.setdefault(op, set()).add(arg)
    closure, todo = checked | expanded, list(expanded)
    done = set(expanded)
    while todo:
        d, e = bodies.get(todo.pop(), (set(), set()))
        closure |= d | e
        todo += e - done
        done |= e
    return closure, {op: args for op, args in asked.items() if op in closure
                     and op not in wrapped and all(_IDENT.fullmatch(a) for a in args)}


def _squash(expr: str) -> str:
    return re.sub(r"\s+", "", expr)


def kept_names(paths, kept: list[str]) -> list[str]:
    """Macros whose definition hangs on a test clang answers for itself: those
    defined inside a group conditioned on one, or on such a macro."""
    texts = {}
    for path in paths:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        if "__has_" in text or "#define" in text:
            texts[path] = text
    names: set[str] = set()
    marks = set(kept)
    while True:
        grown = False
        for path, text in texts.items():
            if not any(m.split("(")[0] in text for m in marks) and not any(n in text for n in names):
                continue
            groups, _ = scan(text)
            lines = text.split("\n")
            for g in groups:
                if not depends_on(groups, g, marks, names):
                    continue
                for line in lines[g.body_first - 1:g.body_last]:
                    m = re.match(r"[ \t]*#[ \t]*define[ \t]+([A-Za-z_]\w*)", line)
                    if m and m.group(1) not in names:
                        names.add(m.group(1))
                        grown = True
        if not grown:
            return sorted(names)


def depends_on_chain(lay: Layout, gid: int, marks: set[str], names: set[str]) -> bool:
    """Whether a group, or one it is nested in, is selected by a kept test."""
    while gid >= 0:
        if depends_on(lay.groups, lay.groups[gid], marks, names):
            return True
        gid = lay.parent[gid]
    return False


def depends_on(groups: list[Group], g: Group, marks: set[str], names: set[str]) -> bool:
    """Whether the condition that selects g asks a kept test or a kept name."""
    for expr in (groups[g.chain].expr, g.expr):
        flat = _squash(expr)
        if any(m in flat for m in marks) or names & set(_IDENT.findall(expr)):
            return True
    return False


def question_text(ops: dict[str, set[str]]) -> tuple[str, list[tuple[str, str]]]:
    pairs = [(op, arg) for op in sorted(ops) for arg in sorted(ops[op])]
    lines = []
    for n, (op, arg) in enumerate(pairs):
        lines += [f"#if defined({op})", f"#if {op}({arg})", f"#define KBL_ANSWER_{n} 1",
                  "#else", f"#define KBL_ANSWER_{n} 0", "#endif", "#endif"]
    return "\n".join(lines) + "\n", pairs


def macros(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        m = re.match(r"#define ([A-Za-z_]\w*)(\([^)]*\))? ?(.*)$", line)
        if m and not m.group(2):
            out[m.group(1)] = m.group(3)
    return out


def _c_integer(text: str) -> int | None:
    m = _INTEGER.fullmatch(text.strip())
    if not m:
        return None
    digits = m[2]
    base = 16 if digits[:2].lower() == "0x" else 8 if digits[:1] == "0" and len(digits) > 1 else 10
    return int(digits, base) * (-1 if m[1] else 1)


def _same(a: str | None, b: str | None) -> bool:
    if a is None or b is None:
        return a is b
    ia, ib = _c_integer(a), _c_integer(b)
    return ia == ib if ia is not None and ib is not None else a.strip() == b.strip()


def identity(build: tuple[str, str], clang: tuple[str, str], closure: set[str],
             pairs: list[tuple[str, str]], own: set[tuple[str, str]]) -> tuple[str, dict]:
    """A forced include holding the build compiler's answers where clang's differ.

    build and clang are each compiler's (predefined macros, answers) output for
    the same questions; own names the pairs only clang's headers ask, which keep
    clang's answer.  An attribute test follows the build compiler both ways,
    since clang ignores an attribute it lacks; any other test follows it only to
    a no, because a yes leads to code clang cannot compile.
    """
    bpre, cpre = macros(build[0]), macros(clang[0])
    bans, cans = macros(build[1]), macros(clang[1])
    lines = []
    meta = {"macros": [], "followed": [], "kept": [], "redefined": []}
    for name in sorted(closure):
        if _same(bpre.get(name), cpre.get(name)):
            continue
        lines.append(f"#undef {name}")
        if name in bpre:
            lines.append(f"#define {name} {bpre[name]}")
        meta["macros"].append(name)
    for op in sorted({op for op, _ in pairs}):
        mine = [(f"KBL_ANSWER_{n}", arg, (o, arg) in own)
                for n, (o, arg) in enumerate(pairs) if o == op]
        if any(k not in cans or (k not in bans and not theirs) for k, _, theirs in mine):
            continue
        table, differs = {}, False
        for k, arg, theirs in mine:
            b, c = bans.get(k), cans[k]
            follow = not theirs and (op.endswith("attribute") or b == "0")
            table[arg] = b if follow else c
            if b is not None and b != c and not theirs:
                (meta["followed"] if follow else meta["kept"]).append(f"{op}({arg})")
                differs = differs or follow
        if not differs:
            continue
        meta["redefined"].append(op)
        lines += [f"#undef {op}", f"#define {op}(x) __kbl_{op.strip('_')}_##x"]
        lines += [f"#define __kbl_{op.strip('_')}_{arg} {v}" for arg, v in sorted(table.items())]
    if not lines:
        return "", meta
    version = bpre.get("__VERSION__", "the build compiler")
    return (f"/* Answers of {version} where clang's differ. */\n" + "\n".join(lines) + "\n",
            meta)


def ask() -> None:
    """Inside the build image: per driver the target it compiles for and its
    own include directory; per flag set its predefined macros and its answers
    to the questions on stdin."""
    spec = json.load(sys.stdin)

    def run(argv, text=None):
        r = subprocess.run(argv, input=text, capture_output=True, text=True, errors="replace")
        return r.stdout if r.returncode == 0 else None

    drivers = {d: [(run([d, "-dumpmachine"]) or "").strip(),
                   (run([d, "-print-file-name=include"]) or "").strip()]
               for d in spec["drivers"]}
    answers = [[run(argv + ["-E", "-dM", "-x", "c", os.devnull]),
                run(argv + ["-E", "-dM", "-x", "c", "-"], spec["questions"])]
               for argv in spec["flag_sets"]]
    json.dump({"drivers": drivers, "answers": answers}, sys.stdout)


if __name__ == "__main__" and sys.argv[1:] == ["collect"]:
    collect()
elif __name__ == "__main__" and sys.argv[1:] == ["ask"]:
    ask()
