#!/usr/bin/env python3
"""Turn a fragment's symbols on in a kernel .config without moving any answer
the .config already has.

A fragment merged the usual way overrides: a line for a symbol the config
holds as a module builds it in, and a line that selects one does the same from
a distance.  Here every symbol answered y or m keeps its answer.  A line is
taken built in when nothing that moves an answer follows from it, as a module
when that is what keeps the answers still, and left off when even that would
not; a line for a symbol this kernel does not define, or defines without a
prompt, is skipped.  So one fragment serves every kernel version, and a config
built around modules keeps them.

The resolution is Kconfig's own: each round writes the requests, runs the
olddefconfig command it was given, and reads back what moved.  What moved is
traced to the symbol that forced it -- through `select`, which raises a module
to built in, or a negated `depends on`, which hides a symbol -- and that symbol
is taken one level down.  Every round lowers some request, so it ends.
"""

import argparse
import os
import re
import shlex
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "index"))
import kbuildinfo  # noqa: E402

_SET = re.compile(r"^CONFIG_(\w+)=(.*)$")
_OFF = re.compile(r"^# CONFIG_(\w+) is not set$")
_RANK = {"n": 0, "m": 1, "y": 2}


def answers(path: str) -> dict[str, str]:
    out: dict[str, str] = {}
    with open(path) as f:
        for line in f:
            line = line.rstrip("\n")
            m = _SET.match(line) or _OFF.match(line)
            if m:
                out[m.group(1)] = m.group(2) if m.re is _SET else "n"
    return out


def wishes(paths: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in paths:
        with open(path) as f:
            for line in f:
                m = _SET.match(line.strip())
                if m and m.group(2) in ("y", "m"):
                    out[m.group(1)] = m.group(2)
    return out


def write(path: str, base: list[str], request: dict[str, str]) -> None:
    keep = []
    for line in base:
        m = _SET.match(line.rstrip("\n")) or _OFF.match(line.rstrip("\n"))
        if not (m and m.group(1) in request):
            keep.append(line)
    with open(path, "w") as f:
        f.writelines(keep)
        for sym, v in sorted(request.items()):
            f.write(f"# CONFIG_{sym} is not set\n" if v == "n" else f"CONFIG_{sym}={v}\n")


_BLOCK = re.compile(r"^(if|endif|menu|endmenu|choice|endchoice|config|menuconfig)\b\s*(.*)$")
_NAME = re.compile(r"\b[A-Z][A-Z0-9_]*\b")


def scopes(src: str) -> dict[str, set[str]]:
    """Per symbol, the names of the `if`, `menu` and `choice` conditions it is
    written inside, within its own file."""
    out: dict[str, set[str]] = {}
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in files:
            if not name.startswith("Kconfig"):
                continue
            try:
                with open(os.path.join(root, name), encoding="utf-8", errors="replace") as f:
                    text = re.sub(r"\\\n[ \t]*", " ", f.read())
            except OSError:
                continue
            stack: list[set[str]] = []
            opener = False
            for line in text.splitlines():
                m = _BLOCK.match(line)
                if m:
                    word, rest = m.groups()
                    opener = word in ("menu", "choice")
                    if word == "if":
                        stack.append(set(_NAME.findall(rest)))
                    elif word in ("menu", "choice"):
                        stack.append(set())
                    elif word in ("endif", "endmenu", "endchoice"):
                        if stack:
                            stack.pop()
                    else:
                        out.setdefault(rest.split()[0] if rest.split() else "", set()).update(
                            *stack)
                    continue
                stripped = line.strip()
                if opener and stack and stripped.startswith(("depends on", "visible if")):
                    stack[-1].update(_NAME.findall(stripped.split(None, 2)[-1]))
    return out


class Kconfig:
    def __init__(self, src: str):
        self.index = kbuildinfo.index_kconfig(src)
        self.scope = scopes(src)
        self.selected_by: dict[str, set[str]] = {}
        for name, syms in self.index.items():
            for s in syms:
                for sel in s.selects:
                    words = sel.split()
                    if words:
                        self.selected_by.setdefault(words[0], set()).add(name)

    def enablers(self, sym: str) -> set[str]:
        """Names whose value decides whether sym can be on at all."""
        own = {n for s in self.index.get(sym, ()) for d in s.depends for n in _NAME.findall(d)}
        return (own | self.scope.get(sym, set())) - {sym}

    def has_prompt(self, sym: str) -> bool:
        return any(s.prompt for s in self.index.get(sym, ()))

    def tristate(self, sym: str) -> bool:
        return any(s.type == "tristate" for s in self.index.get(sym, ()))

    def selects(self, sym: str) -> set[str]:
        return {sel.split()[0] for s in self.index.get(sym, ()) for sel in s.selects if sel.split()}

    def reaches(self, sym: str) -> set[str]:
        seen, todo = set(), [sym]
        while todo:
            for nxt in self.selects(todo.pop()):
                if nxt not in seen:
                    seen.add(nxt)
                    todo.append(nxt)
        return seen

    def blocks(self, sym: str) -> set[str]:
        """Names a `depends on` of sym negates: turning one on hides sym."""
        return {n for s in self.index.get(sym, ()) for d in s.depends
                for n in re.findall(r"!\s*(\w+)", d)}


def olddefconfig(command: list[str], config: str) -> None:
    r = subprocess.run(command + [f"KCONFIG_CONFIG={config}"], stdout=subprocess.DEVNULL)
    if r.returncode:
        raise SystemExit(f"softconfig: olddefconfig failed with status {r.returncode}")


def settle(config: str, fragments: list[str], src: str, command: list[str]) -> dict:
    """Extend config in place; on any failure it is put back as it was."""
    with open(config) as f:
        base = f.readlines()
    try:
        return _settle(config, base, fragments, src, command)
    except BaseException:
        with open(config, "w") as f:
            f.writelines(base)
        raise


def _settle(config: str, base: list[str], fragments: list[str], src: str,
            command: list[str]) -> dict:
    held = {s: v for s, v in answers(config).items() if v in ("y", "m")}
    kc = Kconfig(src)
    wanted = wishes(fragments)
    absent = sorted(s for s in wanted if s not in kc.index)
    promptless = sorted(s for s in wanted if s in kc.index and not kc.has_prompt(s))
    request = {s: v for s, v in wanted.items()
               if s in kc.index and kc.has_prompt(s) and s not in held}
    while True:
        write(config, base, request)
        olddefconfig(command, config)
        got = answers(config)
        moved = {p for p, v in held.items() if got.get(p) != v}
        if not moved:
            break
        fresh = {s for s, v in got.items() if v in ("y", "m") and s not in held}
        forcing = {s: "m" if kc.tristate(s) else "n"
                   for s in fresh if got[s] == "y" and kc.reaches(s) & moved}
        forcing |= {s: "n" for s in fresh for p in moved if s in kc.blocks(p)}
        lower: dict[str, str] = {}
        seen: set[str] = set()

        def down(sym: str, value: str) -> None:
            # A symbol already asked lower that Kconfig still holds up is on by
            # a default nobody can answer; unless something turned on here
            # selects it, lower what made it visible instead.
            if sym in seen:
                return
            seen.add(sym)
            if _RANK[value] < _RANK[request.get(sym, got[sym])]:
                lower[sym] = value
            elif not kc.selected_by.get(sym, set()) & {f for f in fresh if got[f] == "y"}:
                for e in kc.enablers(sym) & fresh:
                    down(e, "m" if kc.tristate(e) and got[e] == "y" else "n")

        for sym, value in forcing.items():
            down(sym, value)
        if not lower:
            raise SystemExit("softconfig: turning the fragment on moves answers the config "
                             f"already had, and no line explains it: {', '.join(sorted(moved))}")
        request.update(lower)
    report = {"kept": [], "on": [], "module": [], "lowered": [], "off": [],
              "absent": absent, "promptless": promptless}
    for s, v in sorted(wanted.items()):
        if s in absent or s in promptless:
            continue
        if s in held:
            report["kept"].append(s)
        elif request.get(s, v) != v:
            report["lowered"].append(s)
        elif got.get(s) == v:
            report["on"].append(s)
        elif got.get(s) in ("y", "m"):
            report["module"].append(s)
        else:
            report["off"].append(s)
    report["added"] = sorted(s for s, v in got.items() if v in ("y", "m") and s not in held)
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", required=True, help="the .config to extend in place")
    ap.add_argument("--source", required=True, help="the kernel source root")
    ap.add_argument("--olddefconfig", required=True,
                    help="the command that runs olddefconfig for this tree, shell-quoted")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("fragments", nargs="+")
    args = ap.parse_args()
    r = settle(os.path.abspath(args.config), args.fragments, args.source,
               shlex.split(args.olddefconfig))
    print(f"{len(r['on'])} on, {len(r['lowered'])} lowered to keep every existing answer, "
          f"{len(r['module'])} as modules their dependencies allow, "
          f"{len(r['kept'])} already set, {len(r['off'])} off for want of a dependency, "
          f"{len(r['absent']) + len(r['promptless'])} not settable in this kernel; "
          f"{len(r['added'])} symbols now set that were not")
    for key, label in (("lowered", "lowered"), ("module", "as modules"), ("off", "off")):
        if r[key]:
            print(f"  {label}: {' '.join(r[key])}")
    if args.verbose:
        for key in ("on", "kept", "absent", "promptless", "added"):
            print(f"  {key}: {' '.join(r[key])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
