#!/usr/bin/env python3
"""What kbuildlab knows about its registered trees, read afresh from each tree
every time: the registry holds only tree.conf, and everything else -- the
kernel version, the commit, whether it is built, which guests run it -- is a
fact of the tree or of the host, so it is looked up rather than remembered.

    treeinfo.py list    [--json] [-q]
    treeinfo.py inspect NAME [--json] [--format KEY]

The registry, data and state directories come from the environment kbuildlab
sets (KBL_TREES, KBL_DATA_HOME, KBL_STATE_DIR)."""

import json
import os
import re
import subprocess
import sys
import time

TREES = os.environ.get("KBL_TREES") or os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "kbuildlab", "trees")
DATA = os.environ.get("KBL_DATA_HOME") or os.path.join(
    os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "kbuildlab")
STATE = os.environ.get("KBL_STATE_DIR") or "/dev/shm"

ARCHES = {"arm64": "arm64", "aarch64": "arm64", "riscv64": "riscv64", "x86_64": "x86_64"}


def _tty():
    return sys.stdout.isatty() and not os.environ.get("NO_COLOR") and os.environ.get("TERM", "dumb") != "dumb"


def _utf8():
    loc = os.environ.get("LC_ALL") or os.environ.get("LC_CTYPE") or os.environ.get("LANG") or ""
    return "utf-8" in loc.lower() or "utf8" in loc.lower()


COLOR = _tty()
OK, NO = ("✓", "✗") if _utf8() else ("+", "x")


def c(code, text):
    return "\033[%sm%s\033[0m" % (code, text) if COLOR else text


def conf_get(tree_dir, key):
    """kbl_tree_get's grammar (lib/common.sh), held to it by tests/tree-conf-vectors.json."""
    line_re = re.compile(rf"^[ \t\r\f\v]*{re.escape(key)}[ \t\r\f\v]*=[ \t\r\f\v]*(.*)$")
    try:
        with open(os.path.join(tree_dir, "tree.conf"), encoding="utf-8", errors="replace") as f:
            for line in f:
                m = line_re.match(line.rstrip("\n"))
                if m:
                    v = m.group(1)
                    if v.startswith('"'):
                        return v[1:].split('"', 1)[0]
                    return v.split("#", 1)[0].rstrip(" \t\r\f\v")
    except OSError:
        pass
    return None


def conf_keys(tree_dir):
    out = []
    try:
        with open(os.path.join(tree_dir, "tree.conf"), encoding="utf-8", errors="replace") as f:
            for line in f:
                m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
                if m and m.group(1) not in out:
                    out.append(m.group(1))
    except OSError:
        pass
    return out


def names():
    try:
        return sorted(d for d in os.listdir(TREES)
                      if os.path.isfile(os.path.join(TREES, d, "tree.conf")))
    except OSError:
        return []


def _git(src, *args):
    try:
        r = subprocess.run(["git", "--no-optional-locks", "-C", src, *args],
                           capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def makefile_version(src):
    v = {}
    try:
        with open(os.path.join(src, "Makefile"), encoding="utf-8", errors="replace") as f:
            for line in f.read(4096).splitlines():
                m = re.match(r"^\s*(VERSION|PATCHLEVEL|SUBLEVEL|EXTRAVERSION)\s*=\s*(.*?)\s*$", line)
                if m:
                    v[m.group(1)] = m.group(2)
    except OSError:
        return None
    if "VERSION" not in v:
        return None
    s = "%s.%s.%s" % (v.get("VERSION", "?"), v.get("PATCHLEVEL", "?"), v.get("SUBLEVEL", "0"))
    return s + v.get("EXTRAVERSION", "")


def _read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return None


def _config_value(cfg, key):
    try:
        with open(cfg, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith(key + "="):
                    return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return None


def _starttime(pid):
    try:
        with open("/proc/%s/stat" % pid) as f:
            rest = f.read().rsplit(") ", 1)[1].split()
        return rest[19]
    except (OSError, IndexError):
        return None


def guests():
    """Live guests started by kbuildlab, by tree directory: the run-state files
    whose recorded qemu is still that process (pid and start time)."""
    out = {}
    try:
        files = [f for f in os.listdir(STATE) if f.startswith("kbl-run-") and f.endswith(".env")]
    except OSError:
        return out
    for f in files:
        st = {}
        try:
            with open(os.path.join(STATE, f), encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    k, _, v = line.rstrip("\n").partition("=")
                    if k.startswith("KBL_"):
                        st[k] = v
        except OSError:
            continue
        pid = st.get("KBL_QEMU_PID")
        if not pid or not os.path.exists("/proc/%s" % pid):
            continue
        if st.get("KBL_QEMU_START") and _starttime(pid) != st.get("KBL_QEMU_START"):
            continue
        tree = os.path.realpath(st.get("KBL_TREE", "")) if st.get("KBL_TREE") else ""
        out.setdefault(tree, []).append({
            "port": int(st["KBL_GDB_PORT"]) if st.get("KBL_GDB_PORT", "").isdigit() else None,
            "pid": int(pid), "boot": st.get("KBL_BOOT") or None,
            "frozen": st.get("KBL_FROZEN") == "1",
            "ssh": int(st["KBL_SSH_PORT"]) if st.get("KBL_SSH_PORT", "").isdigit() else None})
    return out


def _du(path):
    try:
        r = subprocess.run(["du", "-sh", path], capture_output=True, text=True, timeout=60)
        return r.stdout.split()[0] if r.returncode == 0 and r.stdout else None
    except (OSError, subprocess.TimeoutExpired, IndexError):
        return None


def norm_src(value):
    """SRC as lib/common.sh's kbl_norm_src reads it: ~ expanded, then made
    canonical; None when it is empty or relative."""
    if not value:
        return None
    if value == "~" or value.startswith("~/"):
        value = os.path.expanduser(value)
    return os.path.realpath(value) if os.path.isabs(value) else None


def basic(name):
    """What tree.conf alone says, with no look at the source."""
    tdir = os.path.join(TREES, name)
    raw = conf_get(tdir, "SRC")
    arch = ARCHES.get(conf_get(tdir, "ARCH") or "", conf_get(tdir, "ARCH"))
    return {"name": name, "config": tdir, "data": os.path.join(DATA, "trees", name),
            "src": norm_src(raw), "src_stated": raw, "arch": arch,
            "ref": conf_get(tdir, "VERSION"), "upstream": conf_get(tdir, "UPSTREAM") or "stable",
            "boot": conf_get(tdir, "BOOT") or "direct",
            "gdb_port": conf_get(tdir, "GDB_PORT"), "qmp_port": conf_get(tdir, "QMP_PORT"),
            "problems": []}


def info(name, live=None, deep=False):
    d = basic(name)
    tdir, src = d["config"], d["src"] or ""
    arch = d["arch"]
    is_src = bool(src) and os.path.isfile(os.path.join(src, "Makefile")) \
        and os.path.isdir(os.path.join(src, "arch"))
    if not d["src_stated"]:
        d["problems"].append("tree.conf states no SRC")
    elif not src:
        d["problems"].append("SRC is not an absolute path: " + d["src_stated"])
    elif not os.path.isdir(src):
        d["problems"].append("SRC does not exist: " + src)
    elif not is_src:
        d["problems"].append("SRC is not a kernel source: " + src)
    if arch not in ("arm64", "riscv64", "x86_64"):
        d["problems"].append("ARCH is %r" % arch)
    d["version"] = makefile_version(src) if is_src else None
    d["release"] = _read(os.path.join(src, "include/config/kernel.release")) if is_src else None
    git = is_src and os.path.exists(os.path.join(src, ".git"))
    d["commit"] = _git(src, "rev-parse", "HEAD") if git else None
    d["describe"] = _git(src, "describe", "--tags", "--always") if git else None
    d["branch"] = _git(src, "rev-parse", "--abbrev-ref", "HEAD") if git else None
    vm = os.path.join(src, "vmlinux") if is_src else None
    try:
        d["built"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(vm))) if vm else None
    except OSError:
        d["built"] = None
    d["config_present"] = bool(is_src and os.path.isfile(os.path.join(src, ".config")))
    d["compiler"] = _config_value(os.path.join(src, ".config"), "CONFIG_CC_VERSION_TEXT") if is_src else None
    live = guests() if live is None else live
    d["guests"] = live.get(os.path.realpath(tdir), [])
    if deep:
        d["origin"] = _git(src, "remote", "get-url", "origin") if git else None
        d["dirty"] = bool(_git(src, "status", "--porcelain", "--untracked-files=no")) if git else None
        d["data_size"] = _du(d["data"]) if os.path.isdir(d["data"]) else None
        d["settings"] = {k: conf_get(tdir, k) for k in conf_keys(tdir)}
    return d


def _age(stamp):
    return stamp or "-"


def cmd_list(args):
    rows = names()
    if "-q" in args:
        print("\n".join(rows))
        return 0
    live = guests()
    infos = [info(n, live) for n in rows]
    if "--json" in args:
        json.dump(infos, sys.stdout, indent=1)
        print()
        return 0
    if not infos:
        print("no trees are registered (kbuildlab init NAME --arch ARCH, or kbuildlab add DIR)")
        return 0
    head = ["NAME", "ARCH", "KERNEL", "REF", "COMMIT HASH", "BUILT", "GUESTS", "SOURCE"]
    table = []
    for d in infos:
        bad = bool(d["problems"])
        table.append([
            d["name"], d["arch"] or "?", d["release"] or d["version"] or "-", d["ref"] or "-",
            (d["commit"] or "-")[:12], _age(d["built"]), str(len(d["guests"])) if d["guests"] else "-",
            d["src"] or "-", bad])
    width = [max(len(head[i]), *(len(r[i]) for r in table)) for i in range(len(head) - 1)]
    print("  ".join(c("1", h.ljust(width[i])) for i, h in enumerate(head[:-1])) + "  " + c("1", head[-1]))
    for r in table:
        cells = []
        for i, v in enumerate(r[:-1]):
            cell = v.ljust(width[i]) if i < len(width) else v
            if i == 0:
                cell = c("1;31" if r[-1] else "1", cell)
            elif i == 5:
                cell = c("2", cell) if v == "-" else c("32", cell)
            elif i == 6 and v != "-":
                cell = c("33", cell)
            elif i == 4:
                cell = c("36", cell)
            cells.append(cell)
        print("  ".join(cells))
    for d in infos:
        for p in d["problems"]:
            print(c("31", "%s %s: %s" % (NO, d["name"], p)))
    return 0


def _tree_line(prefix, last, key, value):
    branch = ("└─ " if last else "├─ ") if _utf8() else ("`- " if last else "|- ")
    return "%s%s%s %s" % (prefix, c("2", branch), c("36", key.ljust(9)), value)


def cmd_inspect(args):
    if not args:
        print("usage: treeinfo.py inspect NAME [--json] [--format KEY]", file=sys.stderr)
        return 2
    name = args[0]
    if name not in names():
        print("kbuildlab: no tree named '%s'" % name, file=sys.stderr)
        return 1
    if "--format" in args:
        i = args.index("--format")
        key = args[i + 1] if i + 1 < len(args) else ""
        d = basic(name)
        if key not in d or key == "problems":
            tdir = d["config"]
            d = {k: conf_get(tdir, k) for k in conf_keys(tdir)} if key in conf_keys(tdir) \
                else info(name, deep=True)
        v = d.get(key, d.get("settings", {}).get(key))
        if v is None:
            return 1
        print(v if not isinstance(v, (dict, list)) else json.dumps(v))
        return 0
    d = info(name, deep=True)
    if "--json" in args:
        json.dump(d, sys.stdout, indent=1)
        print()
        return 0
    print(c("1", d["name"]) + "  " + (c("32", OK + " ok") if not d["problems"] else
                                      c("31", NO + " " + "; ".join(d["problems"]))))
    git = "%s%s" % (d["describe"] or "-", c("33", " (modified)") if d.get("dirty") else "")
    rows = [
        ("kernel", "%s%s" % (d["version"] or "-", ("  release " + d["release"]) if d["release"] else "")),
        ("arch", d["arch"] or "-"),
        ("source", d["src"] or "-"),
        ("commit", "%s  %s" % (d["commit"] or "-", git)),
        ("tracks", "%s (%s)%s" % (d["ref"] or "-", d["upstream"],
                                  ("  origin " + d["origin"]) if d.get("origin") else "")),
        ("built", "%s%s" % (d["built"] or "no vmlinux", ("  " + d["compiler"]) if d["compiler"] else "")),
        ("boot", "%s  gdb :%s  qmp :%s" % (d["boot"], d["gdb_port"] or "-", d["qmp_port"] or "-")),
        ("config", d["config"] + "/tree.conf"),
        ("data", "%s%s" % (d["data"], ("  " + d["data_size"]) if d.get("data_size") else "  (empty)")),
    ]
    if d["guests"]:
        rows.append(("guests", ", ".join(
            ":%s pid %s %s%s" % (g["port"], g["pid"], g["boot"] or "?", " frozen" if g["frozen"] else "")
            for g in d["guests"])))
    else:
        rows.append(("guests", "none running"))
    for i, (k, v) in enumerate(rows):
        print(_tree_line("", i == len(rows) - 1, k, v))
    return 0


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if argv[0] == "list":
        return cmd_list(argv[1:])
    if argv[0] == "inspect":
        return cmd_inspect(argv[1:])
    if argv[0] == "guests":
        json.dump(guests(), sys.stdout, indent=1)
        print()
        return 0
    print("treeinfo.py: unknown command %r" % argv[0], file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
