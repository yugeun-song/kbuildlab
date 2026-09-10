#!/usr/bin/env python3
"""Pull what the build system knows into the places clangd can show it.

A kernel tree holds a lot that is not C, and clangd reads none of it.  Hover a
CONFIG_ symbol and clang says `#define CONFIG_SLUB 1` -- true, and useless: the
prompt, the type, what it depends on, what selects it, which file it is decided
in, and which sources it turns on all live in Kconfig and in Makefiles.  Hover
_text or __init_begin and the declaration says `extern char _text[]`, while the
address it will have is decided in vmlinux.lds.S.

None of that needs clangd to learn a new language.  A header can carry it as a
comment, so the tree's own headers are copied into an overlay directory with
the build system's knowledge attached, and the overlay is put ahead of the tree
on the include path.  The copy is byte-for-byte apart from inserted comments,
so nothing about the parse changes -- comments do not survive preprocessing --
and the kernel tree itself is never touched.

Where the annotation shows up is worth being exact about, because it is not
where you would first look.  Hover does not show it -- measured, not assumed:
clangd's hover card for a macro is the name, the expansion and the providing
header, and for `extern char _text[], _stext[], _etext[];` it is the type and
the declaration, with the comment above the line left out in both cases.

Go-to-definition is where it lands.  Jump from CONFIG_NUMA and the cursor
arrives in the annotated autoconf.h at the #define, with Kconfig's prompt,
type, dependencies, help text and the Makefile rule directly above it.  Jump
from _stext and it arrives in the annotated sections.h with the vmlinux.lds.S
line and placement above the declaration.  Jump, don't hover.

Two headers are worth intercepting:

  generated/autoconf.h    every CONFIG_ symbol, annotated from Kconfig and from
                          the Makefile rule that makes it build something
  asm-generic/sections.h  the linker-defined symbols, annotated from the
                          architecture's vmlinux.lds.S

Used by setup-clangd.py; runnable on its own to inspect what was extracted:
    ./kbuildinfo.py <kernel-dir> [symbol ...]
"""

from __future__ import annotations

import os
import re
import sys

# ---------------------------------------------------------------------------
# Kconfig
# ---------------------------------------------------------------------------

_TYPES = ("bool", "tristate", "string", "hex", "int",
          "def_bool", "def_tristate")

_CONFIG_START = re.compile(r"^\s*(menuconfig|config)\s+([A-Za-z0-9_]+)\s*$")
_BLOCK_END = re.compile(r"^(config|menuconfig|menu|endmenu|choice|endchoice|"
                        r"if|endif|source|comment|mainmenu)\b")


class KconfigSymbol:
    __slots__ = ("name", "where", "type", "prompt", "depends", "selects",
                 "defaults", "help", "ranges")

    def __init__(self, name: str, where: str):
        self.name = name
        self.where = where
        self.type: str | None = None
        self.prompt: str | None = None
        self.depends: list[str] = []
        self.selects: list[str] = []
        self.defaults: list[str] = []
        self.ranges: list[str] = []
        self.help: list[str] = []


def _strip_prompt(rest: str) -> str | None:
    m = re.match(r'\s*"((?:[^"\\]|\\.)*)"', rest)
    if m:
        return m.group(1)
    m = re.match(r"\s*'((?:[^'\\]|\\.)*)'", rest)
    return m.group(1) if m else None


def parse_kconfig_file(path: str, rel: str) -> list[KconfigSymbol]:
    """Line-oriented, which is all Kconfig needs: entries are introduced by a
    keyword in column zero and their properties are indented under it."""
    out: list[KconfigSymbol] = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return out
    # Kconfig continues a line with a trailing backslash, and a depends-on
    # expression routinely spans three of them.  Join before parsing so the
    # condition is read whole; help text is indented and never uses this.
    text = re.sub(r"\\\n[ \t]*", " ", text)
    lines = text.splitlines()

    i = 0
    n = len(lines)
    while i < n:
        m = _CONFIG_START.match(lines[i])
        if not m:
            i += 1
            continue
        sym = KconfigSymbol(m.group(2), f"{rel}:{i + 1}")
        i += 1
        in_help = False
        help_indent = None
        while i < n:
            line = lines[i]
            if line and not line[0].isspace():
                if _BLOCK_END.match(line):
                    break
                if in_help:
                    break
            stripped = line.strip()
            if in_help:
                if not stripped:
                    sym.help.append("")
                    i += 1
                    continue
                cur = len(line) - len(line.lstrip())
                if help_indent is None:
                    help_indent = cur
                if cur < help_indent:
                    in_help = False
                    continue
                sym.help.append(line[help_indent:] if len(line) > help_indent else stripped)
                i += 1
                continue
            if stripped in ("help", "---help---"):
                in_help = True
                i += 1
                continue
            word, _, rest = stripped.partition(" ")
            if word in _TYPES:
                sym.type = word
                p = _strip_prompt(rest)
                if p:
                    sym.prompt = p
                elif word.startswith("def_"):
                    sym.defaults.append(rest.strip())
            elif word == "prompt":
                sym.prompt = _strip_prompt(rest) or sym.prompt
            elif word == "depends":
                sym.depends.append(re.sub(r"^on\s+", "", rest).strip())
            elif word == "select" or word == "imply":
                sym.selects.append(rest.strip())
            elif word == "default":
                sym.defaults.append(rest.strip())
            elif word == "range":
                sym.ranges.append(rest.strip())
            i += 1
        while sym.help and not sym.help[-1]:
            sym.help.pop()
        out.append(sym)
    return out


def index_kconfig(ksrc: str) -> dict[str, list[KconfigSymbol]]:
    """Every Kconfig entry in the tree.  A symbol can be defined more than
    once -- one per architecture is normal -- and all of them are kept."""
    index: dict[str, list[KconfigSymbol]] = {}
    for root, dirs, files in os.walk(ksrc):
        dirs[:] = [d for d in dirs if d not in (".git", ".cache", "Documentation")]
        for name in files:
            if not name.startswith("Kconfig"):
                continue
            path = os.path.join(root, name)
            rel = os.path.relpath(path, ksrc)
            for sym in parse_kconfig_file(path, rel):
                index.setdefault(sym.name, []).append(sym)
    return index


# ---------------------------------------------------------------------------
# Makefiles: which sources a CONFIG turns on
# ---------------------------------------------------------------------------

_OBJ_RULE = re.compile(
    r"^\s*(?:obj|lib|libs|core|drivers|net|arch)-\$\(CONFIG_([A-Za-z0-9_]+)\)\s*"
    r"(?::?\+?=)\s*(.*)$")
_CONT = re.compile(r"\\\s*$")


def index_makefiles(ksrc: str) -> dict[str, list[str]]:
    """CONFIG_X -> the objects its Makefile rules build, as tree-relative
    paths.  This is the answer to "what does turning this on actually add"."""
    built: dict[str, set[str]] = {}
    for root, dirs, files in os.walk(ksrc):
        dirs[:] = [d for d in dirs if d not in (".git", ".cache", "Documentation")]
        for name in files:
            if name != "Makefile" and not name.startswith("Makefile."):
                continue
            path = os.path.join(root, name)
            reldir = os.path.relpath(root, ksrc)
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    text = f.read()
            except OSError:
                continue
            # Join line continuations so a multi-line rule is seen whole.
            text = re.sub(r"\\\n\s*", " ", text)
            for line in text.splitlines():
                m = _OBJ_RULE.match(line)
                if not m:
                    continue
                cfg = "CONFIG_" + m.group(1)
                for obj in m.group(2).split():
                    if obj.startswith("$") or obj in ("+=", ":="):
                        continue
                    src = obj[:-2] + ".c" if obj.endswith(".o") else obj
                    p = os.path.normpath(os.path.join(reldir, src))
                    built.setdefault(cfg, set()).add(p)
    return {k: sorted(v) for k, v in built.items()}


# ---------------------------------------------------------------------------
# Linker script
# ---------------------------------------------------------------------------

_LDS_SYM = re.compile(r"^\s*(?:PROVIDE(?:_HIDDEN)?\s*\(\s*)?"
                      r"(?:__|_)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*([^;]+);")
_LDS_SECTION = re.compile(r"^\s*([.][A-Za-z0-9_.]+)\s*(?::|\{)")


def parse_linker_script(path: str, rel: str) -> dict[str, tuple[str, str, str]]:
    """symbol -> (where, placement, the assignment as written).

    Whether a symbol sits inside an output section or between two of them is
    the difference between "this marks the start of .text" and "this is where
    the location counter had got to", so brace depth is tracked rather than
    just remembering the last section header seen.  The script is read before
    cpp, so every #ifdef branch is present -- right for documentation, which
    should show each placement a symbol can have and not only this build's."""
    out: dict[str, tuple[str, str, str]] = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return out
    section = None
    section_depth = -1
    depth = 0
    for i, line in enumerate(lines):
        code = re.sub(r"/\*.*?\*/", "", line)
        s = _LDS_SECTION.match(code)
        if s and "{" in code:
            section = s.group(1)
            section_depth = depth
        m = _LDS_SYM.match(code)
        if m:
            name = re.match(r"^\s*(?:PROVIDE(?:_HIDDEN)?\s*\(\s*)?"
                            r"([A-Za-z_][A-Za-z0-9_]*)", code)
            if name:
                inside = section is not None and depth > section_depth
                where = f"in section {section}" if inside else "between sections"
                out[name.group(1)] = (f"{rel}:{i + 1}", where, line.strip().rstrip(";"))
        opened = code.count("{")
        closed = code.count("}")
        depth += opened - closed
        if section is not None and depth <= section_depth:
            section = None
            section_depth = -1
    return out


def index_linker_scripts(ksrc: str, arch: str) -> dict[str, tuple[str, str, str]]:
    out: dict[str, tuple[str, str, str]] = {}
    candidates = [
        f"arch/{arch}/kernel/vmlinux.lds.S",
        "include/asm-generic/vmlinux.lds.h",
    ]
    for rel in candidates:
        p = os.path.join(ksrc, rel)
        if os.path.isfile(p):
            for k, v in parse_linker_script(p, rel).items():
                out.setdefault(k, v)
    return out


# ---------------------------------------------------------------------------
# Overlay headers
# ---------------------------------------------------------------------------

def _safe(text: str) -> str:
    """Kconfig help is prose, and prose contains things like
    /proc/asound/card*/pcm* -- which ends a C block comment three words early
    and turns the rest of the file into a parse error.  A thin space between
    the two characters keeps the text readable and the comment closed."""
    return text.replace("*/", "* /").replace("/*", "/ *")


def _wrap(text: str, width: int = 76) -> list[str]:
    words = text.split()
    lines: list[str] = []
    cur = ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}" if cur else w
    if cur:
        lines.append(cur)
    return lines


def _config_comment(name: str, value: str, syms: list[KconfigSymbol],
                    builds: list[str], help_lines: int) -> list[str]:
    out = ["/**", f" * {_safe(name)} = {_safe(value)}"]
    if not syms:
        out.append(" *")
        out.append(" * No Kconfig entry found for this symbol in this tree.")
        out.append(" */")
        return out
    primary = syms[0]
    if primary.prompt:
        out.append(f" * {_safe(primary.prompt)}")
    out.append(" *")
    kind = primary.type or "unknown type"
    where = ", ".join(s.where for s in syms[:4])
    if len(syms) > 4:
        where += f", and {len(syms) - 4} more"
    out.append(f" * {_safe(kind)}, defined in {_safe(where)}")
    for label, values in (("depends on", primary.depends),
                          ("selects", primary.selects),
                          ("default", primary.defaults),
                          ("range", primary.ranges)):
        for v in values[:4]:
            head = label + ":"
            for chunk in _wrap(_safe(v), 64):
                out.append(f" * {head:<12}{chunk}")
                head = ""
    if builds:
        shown = builds[:8]
        more = f" (+{len(builds) - len(shown)} more)" if len(builds) > len(shown) else ""
        head = "builds:"
        for chunk in _wrap(_safe(", ".join(shown) + more), 64):
            out.append(f" * {head:<12}{chunk}")
            head = ""
    if primary.help:
        out.append(" *")
        body = primary.help[:help_lines]
        for line in body:
            out.append(f" * {_safe(line)}".rstrip())
        if len(primary.help) > help_lines:
            out.append(" * ...")
    out.append(" */")
    return out


_DEFINE = re.compile(r"^#define\s+(CONFIG_[A-Za-z0-9_]+)\s*(.*)$")


def write_autoconf_overlay(ksrc: str, out_path: str, kconfig, makefiles,
                           help_lines: int = 14) -> tuple[int, int]:
    """Copy generated/autoconf.h, attaching a doc comment to every #define."""
    src = os.path.join(ksrc, "include", "generated", "autoconf.h")
    if not os.path.isfile(src):
        return 0, 0
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    total = documented = 0
    with open(src, encoding="utf-8", errors="replace") as f, \
            open(out_path, "w", encoding="utf-8") as w:
        w.write("/*\n * Overlay of the tree's generated/autoconf.h.\n"
                " *\n * Same definitions, with what Kconfig and the Makefiles know about each\n"
                " * symbol attached as a comment so clangd can show it on hover.\n"
                " * Written by setup-clangd.py; the kernel tree is unchanged.\n */\n")
        for line in f:
            m = _DEFINE.match(line)
            if not m:
                w.write(line)
                continue
            total += 1
            name, value = m.group(1), m.group(2).strip()
            base = name[len("CONFIG_"):]
            # CONFIG_FOO=m becomes CONFIG_FOO_MODULE; Kconfig knows it as FOO.
            lookup = base[:-len("_MODULE")] if base.endswith("_MODULE") else base
            syms = kconfig.get(lookup, [])
            if syms:
                documented += 1
            builds = makefiles.get("CONFIG_" + lookup, [])
            for c in _config_comment(name, value or "(defined)", syms, builds, help_lines):
                w.write(c + "\n")
            w.write(line)
    return total, documented


_EXTERN = re.compile(r"^\s*extern\s+.*?\b([A-Za-z_][A-Za-z0-9_]*)\s*\[")


def write_sections_overlay(ksrc: str, out_path: str, lds) -> tuple[int, int]:
    """Copy asm-generic/sections.h, attaching each linker symbol's placement.

    Only comments are inserted, so the declarations clangd sees are exactly the
    tree's own -- no type is restated, and nothing can conflict."""
    src = os.path.join(ksrc, "include", "asm-generic", "sections.h")
    if not os.path.isfile(src):
        return 0, 0
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    total = documented = 0
    with open(src, encoding="utf-8", errors="replace") as f, \
            open(out_path, "w", encoding="utf-8") as w:
        w.write("/*\n * Overlay of the tree's asm-generic/sections.h.\n"
                " *\n * Same declarations, with each symbol's placement in vmlinux.lds.S\n"
                " * attached as a comment.  Written by setup-clangd.py.\n */\n")
        for line in f:
            m = _EXTERN.match(line)
            if not m:
                w.write(line)
                continue
            total += 1
            names = re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\s*\[", line)
            found = [(n, lds[n]) for n in names if n in lds]
            if found:
                documented += 1
                w.write("/**\n")
                for n, (where, section, raw) in found:
                    w.write(f" * {n} -- set at {where}, {_safe(section)}\n")
                    w.write(f" *   {_safe(raw)}\n")
                w.write(" */\n")
            w.write(line)
    return total, documented


# ---------------------------------------------------------------------------
# compiler_types.h: attributes this clang cannot parse
# ---------------------------------------------------------------------------

_COUNTED_BY_DEFINE = re.compile(
    r"^(#\s*define\s+__counted_by(?:_ptr|_le|_be)?\s*\(\s*member\s*\)\s+)"
    r"__attribute__\s*\(\s*\(\s*__counted_by__\s*\(\s*member\s*\)\s*\)\s*\)\s*$")


def clang_handles_forward_counted_by(clang: str = "clang") -> bool:
    """Does this clang accept counted_by naming a member declared later?

    GCC resolves the argument against the completed struct, so the kernel
    writes `char *p __counted_by_ptr(size); int used, size;` and means it.
    clang resolves it where it stands and reports an undeclared identifier,
    which turns one struct definition into a cascade of errors in every file
    that includes it.  Asked rather than assumed, because this is exactly the
    kind of gap a later clang closes.
    """
    import subprocess, tempfile
    fd, src = tempfile.mkstemp(suffix=".c", prefix="counted_by_probe_")
    with os.fdopen(fd, "w") as f:
        f.write("struct s { char *p __attribute__((counted_by(n))); int n; };\n")
    try:
        r = subprocess.run([clang, "-fsyntax-only", src],
                           capture_output=True, text=True)
        return r.returncode == 0
    except OSError:
        return True
    finally:
        try:
            os.unlink(src)
        except OSError:
            pass


def write_compiler_types_overlay(ksrc: str, out_path: str) -> int:
    """Copy linux/compiler_types.h with the counted_by attributes defined away.

    This overlay changes meaning, unlike the other two, so it is worth being
    plain about what it costs: nothing this build cares about.  The tree is
    compiled by GCC, which understands the attribute; the copy exists only for
    the clang that reads the tree, and all the attribute does for a reader is
    let clang bounds-check accesses it cannot bounds-check anyway once the
    struct has failed to parse.  The kernel already spells the fallback
    itself -- `#define __counted_by(member)` with an empty body, for compilers
    without the attribute -- so this is that branch, taken deliberately.

    Done here rather than with -D because the header defines these macros
    itself: a command-line definition is simply overwritten a few lines later.

    Returns the number of definitions neutralised.
    """
    src = os.path.join(ksrc, "include", "linux", "compiler_types.h")
    if not os.path.isfile(src):
        return 0
    changed = 0
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(src, encoding="utf-8", errors="replace") as f, \
            open(out_path, "w", encoding="utf-8") as w:
        w.write("/*\n * Overlay of the tree's linux/compiler_types.h.\n"
                " *\n * Identical except that the counted_by attributes are defined empty:\n"
                " * this clang cannot resolve one that names a member declared later in\n"
                " * the same struct, which GCC -- the compiler that builds this tree --\n"
                " * does.  Written by setup via kbuildinfo.py.\n */\n")
        for line in f:
            m = _COUNTED_BY_DEFINE.match(line.rstrip("\n"))
            if m:
                changed += 1
                w.write(f"{m.group(1).rstrip()}   /* emptied: see overlay header */\n")
                continue
            w.write(line)
    if not changed:
        os.unlink(out_path)
    return changed


def verify_comments(path: str) -> list[str]:
    """An inserted comment that closes early turns the rest of the file into
    code.  Check the property directly rather than trusting the sanitiser:
    scan the file as a C preprocessor would and report any line where a
    generated comment does not stay a comment."""
    problems: list[str] = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return [f"{path}: unreadable"]
    in_block = False
    for i, line in enumerate(lines, 1):
        rest = line
        while rest:
            if in_block:
                end = rest.find("*/")
                if end < 0:
                    break
                rest = rest[end + 2:]
                in_block = False
                # Anything after a comment closes on a line that started as
                # comment body is text we did not mean to emit as code.
                if rest.strip() and not rest.lstrip().startswith(("#", "/*", "//")):
                    problems.append(f"{path}:{i}: comment closed early: {line.strip()[:70]}")
                continue
            start = rest.find("/*")
            line_c = rest.find("//")
            if start < 0 or (0 <= line_c < start):
                break
            rest = rest[start + 2:]
            in_block = True
    if in_block:
        problems.append(f"{path}: file ends inside a comment")
    return problems


def build_overlay(ksrc: str, overlay_dir: str, arch: str, verbose: bool = False):
    """Write the overlay tree and report what went into it."""
    kconfig = index_kconfig(ksrc)
    makefiles = index_makefiles(ksrc)
    lds = index_linker_scripts(ksrc, arch)
    ac_total, ac_doc = write_autoconf_overlay(
        ksrc, os.path.join(overlay_dir, "generated", "autoconf.h"), kconfig, makefiles)
    se_total, se_doc = write_sections_overlay(
        ksrc, os.path.join(overlay_dir, "asm-generic", "sections.h"), lds)
    counted = 0
    if not clang_handles_forward_counted_by():
        counted = write_compiler_types_overlay(
            ksrc, os.path.join(overlay_dir, "linux", "compiler_types.h"))
    problems: list[str] = []
    for rel in ("generated/autoconf.h", "asm-generic/sections.h"):
        p = os.path.join(overlay_dir, rel)
        if os.path.isfile(p):
            problems += verify_comments(p)
    if problems:
        for line in problems[:10]:
            print(f"    OVERLAY PROBLEM {line}")
        raise SystemExit(f"overlay for {ksrc} would break the parse "
                         f"({len(problems)} bad comment(s)); refusing to use it")
    if verbose:
        print(f"    Kconfig: {len(kconfig)} symbols, "
              f"Makefiles: {len(makefiles)} config-gated build rules, "
              f"linker script: {len(lds)} symbols")
        print(f"    overlay: {ac_doc}/{ac_total} CONFIG defines documented, "
              f"{se_doc}/{se_total} section declarations documented"
              + (f", {counted} counted_by attribute(s) defined away" if counted else ""))
    return {"kconfig": len(kconfig), "makefile_rules": len(makefiles),
            "counted_by_neutralised": counted,
            "lds_symbols": len(lds), "config_defines": ac_total,
            "config_documented": ac_doc, "section_decls": se_total,
            "section_documented": se_doc}


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    ksrc = os.path.abspath(sys.argv[1])
    kconfig = index_kconfig(ksrc)
    makefiles = index_makefiles(ksrc)
    print(f"{len(kconfig)} Kconfig symbols, {len(makefiles)} config-gated rules")
    for name in sys.argv[2:]:
        base = name[len("CONFIG_"):] if name.startswith("CONFIG_") else name
        syms = kconfig.get(base, [])
        builds = makefiles.get("CONFIG_" + base, [])
        print()
        print("\n".join(_config_comment("CONFIG_" + base, "?", syms, builds, 20)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
