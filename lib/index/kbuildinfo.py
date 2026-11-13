#!/usr/bin/env python3
"""What the build system knows and clang does not: the tree's Kconfig entries, and
whether this clang parses the way the tree uses counted_by.
"""

from __future__ import annotations

import os
import re

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
# attributes this clang cannot parse
# ---------------------------------------------------------------------------

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
