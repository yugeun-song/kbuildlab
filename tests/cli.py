#!/usr/bin/env python3
"""Hold lib/cli.py -- what --help prints and Tab completes -- against the code
that parses each command: the same options, every dispatched command described,
every described command dispatched and in the usage text.  Reads the sources
only; nothing is run."""

import os
import re
import sys

here = os.path.dirname(os.path.abspath(__file__))
repo = os.path.dirname(here)
sys.path.insert(0, os.path.join(repo, "lib"))
import cli  # noqa: E402

SHELL = [os.path.join(repo, "bin", "kbuildlab")] + [
    os.path.join(repo, "lib", f) for f in sorted(os.listdir(os.path.join(repo, "lib"))) if f.endswith(".sh")]
SCRIPTS = {"run": "run-qemu.sh", "attach": "run-gdb.sh", "drgn": "run-drgn.sh", "resume": "resume.sh"}
EXTRA = {"containers": ["_containers_clean"]}
OPT = re.compile(r'(?:^[ \t]*|\bin[ \t]+)(-{1,2}[A-Za-z][-A-Za-z0-9]*(?:\|-{1,2}[A-Za-z][-A-Za-z0-9]*)*)\)',
                 re.M)
TEST = re.compile(r'==\s*(-{1,2}[A-Za-z][-A-Za-z0-9]*)')
IGNORED = {"-h", "--help"}

failed = 0


def fail(msg):
    global failed
    failed += 1
    print("FAIL " + msg)


def source():
    return "".join(open(p, encoding="utf-8").read() for p in SHELL)


def function(src, name):
    m = re.search(r'^%s\(\) \{\n(.*?)^\}' % re.escape(name), src, re.S | re.M)
    return m.group(1) if m else None


def arg_loop(path):
    s = open(path, encoding="utf-8").read()
    m = re.search(r'while \[\[ \$# -gt 0 \]\]; do\s*\n\s*case "\$1" in\n(.*?)\n\s*esac', s, re.S)
    return m.group(1) if m else ""


def options(text):
    text = re.sub(r'"[^"\n]*"', '""', "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#")))
    found = set()
    for m in OPT.finditer(text):
        found.update(m.group(1).split("|"))
    found.update(TEST.findall(text))
    return found - IGNORED


src = source()
for c in cli.COMMANDS:
    if c.name == "help":
        continue
    if c.name in SCRIPTS:
        parsed = options(arg_loop(os.path.join(repo, "lib", SCRIPTS[c.name])))
    else:
        body = function(src, "cmd_" + c.name)
        if body is None:
            fail("%s: no cmd_%s in bin/kbuildlab or lib/*.sh" % (c.name, c.name))
            continue
        parsed = options(body)
        for extra in EXTRA.get(c.name, []):
            parsed |= options(function(src, extra) or "")
    described = {n for o in c.opts for n in o.names}
    if parsed != described:
        fail("%s: parsed %s, described %s" % (c.name, sorted(parsed - described) or "-",
                                              sorted(described - parsed) or "-"))

dispatch = re.search(r'^\{\ncmd=.*?^esac\nexit', src, re.S | re.M).group(0)
dispatched = set()
for m in re.finditer(r'^\s{4}([a-z][a-z|]*)\)\s+cmd_', dispatch, re.M):
    dispatched.update(m.group(1).split("|"))
described = {n for c in cli.COMMANDS for n in [c.name] + c.aliases} - {"help"}
if dispatched != described:
    fail("dispatch %s vs cli.py %s" % (sorted(dispatched - described) or "-",
                                       sorted(described - dispatched) or "-"))
usage = function(src, "usage") or ""
for c in cli.COMMANDS:
    if not re.search(r'kbuildlab %s\b' % re.escape(c.name), usage):
        fail("usage does not mention kbuildlab %s" % c.name)

for c in cli.COMMANDS:
    try:
        cli.render_help(c)
    except Exception as e:  # noqa: BLE001
        fail("%s: --help does not render (%s)" % (c.name, e))

print("%d commands checked, %s" % (len(cli.COMMANDS), "all agree" if not failed else "%d failed" % failed))
sys.exit(1 if failed else 0)
