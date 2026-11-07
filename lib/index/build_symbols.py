#!/usr/bin/env python3
"""What the build defined and ctags did not see, added to tags: build_symbols.py

Run in the build image at the top of the tree, with KBL_TAGS_STATE naming the
run's state directory.  ctags reads text, so it misses a variable followed by an
attribute, anything a macro call makes, and .req.  The objects know: a C object's
debug info holds each definition's declaration line, and an assembler unit's
preprocessed text holds its labels at their source lines.  Line tables are not
used: over a relocatable object, nm -l put most riscv functions far from their lines.

Lines this adds end in origin:build, so the next ctags merge can drop them and this
run decides them again.  Each object's answer is kept until the object changes.
"""

from __future__ import annotations

import concurrent.futures as cf
import hashlib
import json
import multiprocessing
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import clangd
import inactive_regions as ir
import tagview

MARK = b"\torigin:build"
MEMO = "symbols.json"
NAME = re.compile(r"[A-Za-z_]\w*\Z")
WORD = re.compile(r"[A-Za-z_]\w*")
# What defines a symbol in tags, by kind.  A C member or type shares no namespace
# with it, and a .globl is listed as s.
DEFINING_C = {b"f", b"v", b"d", b"l", b"A"}
DEFINING_ASM = DEFINING_C | {b"m"}

DIE = re.compile(r"\s*<(\d+)><([0-9a-f]+)>: Abbrev Number: \d+ \((DW_TAG_\w+)\)")
ATTR = re.compile(r"\s*<[0-9a-f]+>\s+(DW_AT_\w+)\s*:\s*(.*)")
TABLE_ROW = re.compile(r"\s+(\d+)\s+(.*)")
MARKER = re.compile(r'#\s*(\d+)\s+"([^"]*)"')
DEFINE = re.compile(r"\s*#\s*define\s+([A-Za-z_]\w*)")
LABEL = re.compile(r"\s*([\w.$]+)\s*:")
ASSIGNED = re.compile(r"\s*([A-Za-z_]\w*)\s*==?(?!=)")
ALIAS = re.compile(r"\s*([A-Za-z_]\w*)\s+\.(?:req|qn|dn)\b")
SET = re.compile(r"\s*\.(?:set|equ|equiv|eqv)\s+([A-Za-z_]\w*)")
COMMON = re.compile(r"\s*\.l?comm\s+([A-Za-z_]\w*)")
MACRO = re.compile(r"\s*\.macro\s+([A-Za-z_][\w$.]*)[\s,]*(.*)")
MACRO_END = re.compile(r"\s*\.endm\b")
PARAM = re.compile(r"([A-Za-z_][\w$]*)(?::\w+)?(?:\s*=\s*[^\s,]+)?")
MADE_LABEL = re.compile(r"\\(\w+)(?:\\\(\))?\s*:")
OPERATION = re.compile(r"\s*([A-Za-z_][\w$.]*)\s+(.*)")

tagged: dict[str, set[str]] = {}
listed: set[str] = set()
root = ""


def elf_symbols(path: str) -> dict[str, tuple[str, bool]]:
    """Every symbol an object defines: its ELF type, and whether it is local."""
    out = subprocess.run(["readelf", "--syms", "--wide", path], capture_output=True, text=True, errors="replace")
    symbols: dict[str, tuple[str, bool]] = {}
    for line in out.stdout.splitlines():
        p = line.split()
        if len(p) >= 8 and p[0].endswith(":") and p[-2] != "UND" and p[3] not in ("SECTION", "FILE") and NAME.match(p[-1]):
            symbols[p[-1]] = (p[3], p[4] == "LOCAL")
    return symbols


def line_table_files(path: str, comp_dir: str | None) -> dict[int, str]:
    """The first unit's file table, tree-relative; DWARF 4 and 5 lay it out alike."""
    proc = subprocess.Popen(["readelf", "--debug-dump=rawline", path], stdout=subprocess.PIPE, text=True,
                            stderr=subprocess.DEVNULL, errors="replace")
    dirs: dict[int, str] = {}
    files: dict[int, tuple[int, str]] = {}
    table = None
    assert proc.stdout is not None
    for line in proc.stdout:
        if "The Directory Table" in line:
            table = dirs
        elif "The File Name Table" in line:
            table = files
        elif table is not None and not line.strip():
            if table is files:
                break
            table = None
        elif table is not None:
            m = TABLE_ROW.match(line)
            if not m:
                continue
            value = m[2].rsplit("): ", 1)[-1] if "(indirect" in m[2] else m[2].rsplit("\t", 1)[-1]
            if table is dirs:
                dirs[int(m[1])] = value
            else:
                d = m[2].split()[0]
                files[int(m[1])] = (int(d) if d.isdigit() else 0, value)
    proc.stdout.close()
    proc.kill()
    proc.wait()
    base = dirs.get(0) or comp_dir or root
    out = {}
    for index, (d, name) in files.items():
        full = os.path.normpath(os.path.join(base, dirs.get(d, ""), name) if d else os.path.join(base, name))
        rel = os.path.relpath(full, root)
        if not rel.startswith(".."):
            out[index] = rel
    return out


def declared_lines(path: str) -> dict[str, tuple[str, int]]:
    """Where a C object's debug info declares each definition at file scope."""
    out = subprocess.run(["readelf", "--debug-dump=info", "--dwarf-depth=2", path], capture_output=True, text=True,
                         errors="replace")
    dies: dict[str, dict[str, str]] = {}
    order: list[str] = []
    die = None
    comp_dir = None
    units = 0
    for line in out.stdout.splitlines():
        if "Compilation Unit @" in line:
            units += 1
            if units > 1:
                break
            continue
        m = DIE.match(line)
        if m:
            die = None
            if m[1] == "1" and m[3] in ("DW_TAG_subprogram", "DW_TAG_variable"):
                die = {}
                dies[m[2]] = die
                order.append(m[2])
            elif m[1] == "0":
                die = {"unit": "1"}
            continue
        if die is None:
            continue
        a = ATTR.match(line)
        if not a:
            continue
        value = a[2].rsplit("): ", 1)[-1] if a[2].startswith("(indirect") else a[2]
        if a[1] == "DW_AT_comp_dir" and "unit" in die:
            comp_dir = value
        elif a[1] in ("DW_AT_name", "DW_AT_linkage_name", "DW_AT_decl_file", "DW_AT_decl_line", "DW_AT_declaration",
                      "DW_AT_specification", "DW_AT_abstract_origin"):
            die[a[1][6:]] = value
    table = None
    found: dict[str, tuple[str, int]] = {}
    for offset in order:
        d = dies[offset]
        if "declaration" in d:
            continue
        ref = d.get("specification") or d.get("abstract_origin")
        origin = dies.get(ref.strip("<>").removeprefix("0x"), {}) if ref else {}
        name = d.get("linkage_name") or d.get("name") or origin.get("linkage_name") or origin.get("name")
        number = d.get("decl_file") or origin.get("decl_file")
        lineno = d.get("decl_line") or origin.get("decl_line")
        if not (name and number and lineno and number.split()[0].isdigit() and lineno.split()[0].isdigit()):
            continue
        if table is None:
            table = line_table_files(path, comp_dir)
        rel = table.get(int(number.split()[0]))
        if rel:
            found[name] = (rel, int(lineno.split()[0]))
    return found


def assembler_definitions(text: str, symbols) -> list[tuple[str, str, int, str]]:
    """(name, file, line, form) for what a preprocessed assembler unit defines; form
    is define, macro or symbol.  A macro's argument counts when the macro's body makes
    a label of that parameter, directly or through another macro."""
    out: list[tuple[str, str, int, str]] = []
    macros: dict[str, tuple[list[str], set[str]]] = {}
    body = None
    place, number = "", 0

    def arguments(operation: str, rest: str):
        params = macros[operation][0]
        for i, arg in enumerate(x for x in re.split(r"[\s,]+", rest) if x):
            key, _, value = arg.partition("=")
            if value and key in params:
                yield key, value
            elif not value and i < len(params):
                yield params[i], arg

    for raw in text.split("\n"):
        m = MARKER.match(raw)
        if m:
            place, number = m[2], int(m[1])
            continue
        if raw.lstrip().startswith("#"):
            d = DEFINE.match(raw)
            if d:
                out.append((d[1], place, number, "define"))
            number += 1
            continue
        for statement in raw.split(";"):
            while True:
                lab = LABEL.match(statement)
                if not lab:
                    break
                if lab[1] in symbols:
                    out.append((lab[1], place, number, "symbol"))
                statement = statement[lab.end():]
            if body is not None:
                if MACRO_END.match(statement):
                    macros[body[0]] = (body[1], body[2])
                    body = None
                    continue
                body[2].update(x for x in MADE_LABEL.findall(statement) if x in body[1])
                op = OPERATION.match(statement)
                if op and op[1] in macros:
                    for param, arg in arguments(op[1], op[2]):
                        if param in macros[op[1]][1] and arg.startswith("\\") and arg[1:] in body[1]:
                            body[2].add(arg[1:])
                continue
            mac = MACRO.match(statement)
            if mac:
                out.append((mac[1], place, number, "macro"))
                body = (mac[1], [p[1] for p in PARAM.finditer(mac[2])], set())
                continue
            for pattern, form in ((ALIAS, "define"), (SET, "define"), (ASSIGNED, "define"), (COMMON, "symbol")):
                hit = pattern.match(statement)
                if hit:
                    out.append((hit[1], place, number, form))
                    break
            else:
                op = OPERATION.match(statement)
                if op and op[1] in macros:
                    for param, arg in arguments(op[1], op[2]):
                        if param in macros[op[1]][1] and arg in symbols:
                            out.append((arg, place, number, "symbol"))
        number += 1
    return out


def spelled_at(name: str, rel: str, lineno: int) -> int:
    """A macro call over several lines reaches the preprocessor's output as one, at
    its first line; the name's own line is further down."""
    try:
        with open(os.path.join(root, rel), errors="replace") as f:
            lines = f.read().split("\n")
    except OSError:
        return lineno
    word = re.compile(rf"(?<![\w$.]){re.escape(name)}(?![\w$])")
    if 0 < lineno <= len(lines) and word.search(lines[lineno - 1]):
        return lineno
    label = re.compile(rf"(?<![\w$.]){re.escape(name)}\s*:")
    continued = lineno
    while continued <= len(lines) and lines[continued - 1].rstrip().endswith(("\\", ",", "(")):
        continued += 1
        if label.search(lines[continued - 1] if continued <= len(lines) else ""):
            return continued
    return lineno


def is_assembler(rel: str) -> bool:
    return rel.endswith((".S", ".s", ".sx"))


def symbol_kind(elf_type: str) -> str:
    return "f" if elf_type == "FUNC" else "v"


def first_spelled(names, path: str) -> dict[str, int]:
    """The line each name first appears on in a C source, outside comments."""
    with open(path, errors="replace") as f:
        text = re.sub(r"/\*.*?\*/|//[^\n]*", lambda m: "\n" * m[0].count("\n"), f.read(), flags=re.S)
    found: dict[str, int] = {}
    for lineno, line in enumerate(text.split("\n"), 1):
        for word in WORD.findall(line):
            if word in names:
                found.setdefault(word, lineno)
    return found


def c_unit(unit: dict) -> list[list]:
    path = os.path.join(root, unit["target"])
    source = os.path.relpath(unit["file"], root)
    read = {source} | {os.path.normpath(d) for d in clangd.kbuild_deps(unit["cmd"]) or ()}
    wanted = {n: v for n, v in elf_symbols(path).items() if not tagged.get(n, set()) & read}
    if not wanted:
        return []
    declared = declared_lines(path)
    out = [[name, rel, lineno, symbol_kind(wanted[name][0]), wanted[name][1]]
           for name, (rel, lineno) in declared.items() if name in wanted and rel in listed]
    # Inline assembler makes symbols the debug info does not know (ASM_EXPORT).
    undeclared = wanted.keys() - declared.keys()
    if undeclared:
        out += [[name, source, lineno, symbol_kind(wanted[name][0]), wanted[name][1]]
                for name, lineno in first_spelled(undeclared, unit["file"]).items()]
    return out


def assembler_unit(unit: dict) -> list[list]:
    symbols = elf_symbols(os.path.join(root, unit["target"]))
    argv = [a for a in ir.unit_argv(unit["command"]) if a != "-fdirectives-only"]
    r = subprocess.run(argv, cwd=root, capture_output=True, text=True, errors="replace")
    if r.returncode:
        raise RuntimeError(r.stderr.strip()[-300:])
    out, seen = [], set()
    for name, place, lineno, form in assembler_definitions(r.stdout, symbols):
        if not NAME.match(name) or not place or place.startswith("<"):
            continue
        rel = os.path.relpath(os.path.normpath(os.path.join(root, place)), root)
        if rel not in listed or rel in tagged.get(name, ()):
            continue
        if form == "symbol":
            lineno = spelled_at(name, rel, lineno)
        if (name, rel, lineno) in seen:
            continue
        seen.add((name, rel, lineno))
        elf_type, local = symbols.get(name, ("", False))
        if form == "macro":
            kind = "m" if is_assembler(rel) else "A"
        else:
            kind = "d" if form == "define" else symbol_kind(elf_type)
        out.append([name, rel, lineno, kind, local])
    return out


def one_unit(unit: dict):
    try:
        read = assembler_unit if is_assembler(unit["file"]) else c_unit
        return read(unit), None
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as e:
        return None, f"{unit['target']}: {e}"


def code_key() -> str:
    h = hashlib.sha1()
    with open(os.path.abspath(__file__), "rb") as f:
        h.update(f.read())
    return h.hexdigest()


def stamp(path: str) -> list | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return [st.st_size, st.st_mtime_ns]


def tag_line(name: str, rel: str, lineno: int, kind: str, local: bool) -> bytes:
    scope = b"\tfile:" if local else b""
    return b'%b\t%b\t%d;"\t%b%b%b\n' % (name.encode(), rel.encode(), lineno, kind.encode(), scope, MARK)


def main() -> int:
    global root
    state = os.environ["KBL_TAGS_STATE"]
    root = os.getcwd()
    with open("tags", "rb") as f:
        ctags_lines = [line for line in f.read().split(b"\n") if line and not line.endswith(MARK)]
    for line in ctags_lines:
        if line.startswith(b"!_TAG_"):
            continue
        p = line.split(b"\t", 4)
        if len(p) >= 4 and p[3] in (DEFINING_ASM if p[1].endswith((b".S", b".s", b".sx")) else DEFINING_C):
            name, rel = (x.decode(errors="surrogateescape") for x in p[:2])
            tagged.setdefault(name, set()).add(sys.intern(rel))
    with open(os.path.join(state, "keys")) as f:
        keys = f.read().split()
    for key in keys:
        with open(os.path.join(state, f"files.{key}"), errors="surrogateescape") as f:
            listed.update(os.path.normpath(x) for x in f.read().split("\n") if x)
    units = [u for u in tagview.recorded_units(root)
             if u["target"].endswith(".o") and os.path.relpath(u["file"], root) in listed
             and os.path.exists(os.path.join(root, u["target"]))]

    path = os.path.join(state, MEMO)
    code = code_key()
    try:
        with open(path) as f:
            memo = json.load(f)
        if memo.get("code") != code:
            memo = {}
    except (OSError, ValueError):
        memo = {}
    known = memo.get("units", {})
    results: dict[str, dict] = {}
    todo = []
    for u in units:
        now = stamp(os.path.join(root, u["target"]))
        kept = known.get(u["target"])
        if kept and kept["stamp"] == now:
            results[u["target"]] = kept
        else:
            todo.append((u, now))

    failed = []
    if todo:
        jobs = clangd.auto_jobs(cap=int(os.environ.get("KBL_JOBS", "0")) or None, job_mb=clangd.PREPROCESS_MB)
        with cf.ProcessPoolExecutor(jobs, mp_context=multiprocessing.get_context("fork")) as ex:
            for (u, now), (entries, error) in zip(todo, ex.map(one_unit, [u for u, _ in todo], chunksize=8)):
                if error:
                    failed.append(error)
                else:
                    results[u["target"]] = {"stamp": now, "entries": entries}
    tmp = path + ".kbl-tmp"
    with open(tmp, "w") as f:
        json.dump({"code": code, "units": results}, f)
    os.replace(tmp, path)

    added: dict[tuple[str, str, int], list] = {}
    for result in results.values():
        for name, rel, lineno, kind, local in result["entries"]:
            if rel not in tagged.get(name, ()):
                added.setdefault((name, rel, lineno), [name, rel, lineno, kind, local])
    new = [tag_line(*e) for e in added.values()]
    with open("tags.kbl-tmp", "wb") as f:
        f.write(b"".join(sorted([line + b"\n" for line in ctags_lines] + new)))
    os.replace("tags.kbl-tmp", "tags")
    print(f"symbols: {len(units)} objects ({len(units) - len(todo)} carried over), "
          f"{len(new)} definitions ctags did not see added" + (f", {len(failed)} objects unread" if failed else ""),
          flush=True)
    for error in failed[:5]:
        print(f"symbols: unread {error}", file=sys.stderr)
    return 1 if todo and len(failed) == len(todo) else 0


if __name__ == "__main__":
    sys.exit(main())
