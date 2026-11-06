#!/usr/bin/env python3
"""What a build no longer makes, and what a failed incremental build tripped on.

kbuild never removes an output whose rule is gone, so an object from before a
config change or a sync keeps its .cmd, and whatever reads those records takes
it for part of the build.  A wrong removal costs one recompile, because make
rebuilds whatever it still needs; a source file is never touched.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

RECORD = re.compile(r"(?:saved)?cmd_(\S+) := (.*)")
WORD = re.compile(r"[\s'\";:=,()@|<>]+")
REMOVABLE = (".o", ".ko", ".mod")
BUILT_PIECES = (".o", ".a", ".ko", ".mod")
FAILED_TARGET = re.compile(r"\*\*\* \[[^\]]*?: ([^\]:]+)\] Error")
NEEDED_BY = re.compile(r"needed by '([^']+)'")
COMPILER_ERROR = re.compile(r"^([^\s:]+):\d+(?::\d+)?: (?:fatal )?error:")
PROGRESS = re.compile(r"\s+[A-Z][A-Z0-9_]*\s+\S|make(?:\[\d+\])?: ")
OBJECT = re.compile(r"[\w./+-]+\.(?:o|a|ko)\b")


def records(ksrc: str) -> dict[str, str]:
    """Each output kbuild keeps a record of, and the command that made it.  A
    separate build under tools/ names its outputs differently and stays out."""
    out = {}
    for d, dirs, files in os.walk(ksrc):
        dirs[:] = [x for x in dirs if x != ".git"]
        for name in files:
            if not (name.startswith(".") and name.endswith(".cmd")):
                continue
            target = os.path.relpath(os.path.join(d, name[1:-4]), ksrc)
            try:
                with open(os.path.join(d, name), errors="replace") as f:
                    m = RECORD.match(f.readline())
            except OSError:
                continue
            if m and os.path.normpath(m[1]) == target:
                out[target] = m[2]
    return out


def thin_members(ksrc: str, archive: str) -> list[str] | None:
    """The members a thin archive names; None for any other file."""
    try:
        with open(os.path.join(ksrc, archive), "rb") as f:
            if f.read(8) != b"!<thin>\n":
                return None
            data = f.read()
    except OSError:
        return None
    at = data.find(b"//")
    if at < 0:
        return []
    names = data[at + 60:at + 60 + int(data[at + 48:at + 58])]
    return [os.path.normpath(os.path.join(os.path.dirname(archive), n.decode("latin1")))
            for n in names.split(b"/\n") if n.strip()]


def words(text: str) -> list[str]:
    return [os.path.normpath(w) for w in WORD.split(text) if w]


def read_text(path: str) -> str:
    try:
        with open(path, errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def reached(ksrc: str, recs: dict[str, str]) -> set[str] | None:
    """What the products of the build reach: the thin vmlinux.a, modules.order
    and the inputs every other record names.  None when the tree has no thin
    vmlinux.a, which is what makes the answer complete."""
    if thin_members(ksrc, "vmlinux.a") is None:
        return None
    todo = ["vmlinux.a"] + [t for t in recs if not t.endswith(BUILT_PIECES)]
    for line in read_text(os.path.join(ksrc, "modules.order")).split("\n"):
        if line:
            todo.append(os.path.normpath(line[:-2] + ".ko" if line.endswith(".o") else line))
    seen: set[str] = set()
    scripts: set[str] = set()
    while todo:
        t = todo.pop()
        if t in seen:
            continue
        seen.add(t)
        path = os.path.join(ksrc, t)
        names = words(recs.get(t, ""))
        if t.endswith(".a"):
            names += thin_members(ksrc, t) or []
        elif t.endswith(".mod"):
            names += words(read_text(path))
        elif t.endswith(".o"):
            names.append(t[:-2] + ".mod")
        for w in names:
            if w.endswith(".sh") and w not in scripts:
                scripts.add(w)
                names = names + words(read_text(os.path.join(ksrc, w)))
        todo += [w for w in names if w not in seen]
    return seen


def forget(ksrc: str, target: str) -> None:
    d, base = os.path.split(os.path.join(ksrc, target))
    for path in (os.path.join(d, base), os.path.join(d, f".{base}.cmd")):
        try:
            os.unlink(path)
        except OSError:
            pass


def stale_outputs(ksrc: str) -> list[str]:
    """Most of a build being stale says the reading is wrong, not the build."""
    recs = records(ksrc)
    live = reached(ksrc, recs)
    if live is None:
        return []
    gone = sorted(t for t in recs if t.endswith(REMOVABLE) and t not in live)
    return [] if len(gone) * 2 > sum(t.endswith(REMOVABLE) for t in recs) else gone


def generated(ksrc: str, paths: set[str]) -> set[str]:
    """Those of these files git ignores.  Nothing else may ever be removed, so
    a tree git cannot answer for gives up nothing."""
    if not paths:
        return set()
    r = subprocess.run(["git", "-C", ksrc, "check-ignore", "--stdin", "-z"],
                       input="\0".join(sorted(paths)) + "\0", capture_output=True, text=True)
    return {p for p in r.stdout.split("\0") if p} if r.returncode in (0, 1) else set()


def failed_outputs(ksrc: str, log: str) -> list[str]:
    """The generated files a failed build names as what it could not make or read."""
    named: set[str] = set()
    for line in log.split("\n"):
        m = COMPILER_ERROR.match(line)
        hits = [m[1]] if m else []
        hits += FAILED_TARGET.findall(line) + NEEDED_BY.findall(line)
        if not PROGRESS.match(line):
            hits += OBJECT.findall(line)
        named |= {os.path.normpath(h) for h in hits}
    files = {n for n in named if not os.path.isabs(n) and not n.startswith("..")
             and os.path.isfile(os.path.join(ksrc, n))}
    return sorted(generated(ksrc, files))


def failed_directories(ksrc: str, log: str) -> list[str]:
    """Every output kbuild recorded beside what a failed build could not make."""
    dirs = {os.path.dirname(os.path.normpath(t)) for t in FAILED_TARGET.findall(log)}
    dirs.discard("")
    if not dirs:
        return []
    return sorted(generated(ksrc, {t for t in records(ksrc) if os.path.dirname(t) in dirs}))


def main() -> int:
    action, ksrc = sys.argv[1], os.path.abspath(sys.argv[2])
    if action == "stale":
        gone = stale_outputs(ksrc)
    elif action == "failed":
        gone = failed_outputs(ksrc, read_text(sys.argv[3]))
    elif action == "failed-directories":
        gone = failed_directories(ksrc, read_text(sys.argv[3]))
    else:
        raise SystemExit(f"build_outputs.py: unknown action {action}")
    for target in gone:
        forget(ksrc, target)
        print(target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
