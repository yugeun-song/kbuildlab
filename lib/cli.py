#!/usr/bin/env python3
"""kbuildlab's command line, described once: `kbuildlab CMD --help` prints a
command from here, and the shell completions ask it what may come next.

    cli.py help CMD
    cli.py complete WORD...    the words after `kbuildlab`; the last is the word
                               being completed, empty when a new one starts

run, attach, drgn and resume print their own help (lib/run-*.sh, lib/resume.sh)
and are described here for completion only.  tests/cli.py holds every option
here against the parser that reads it, so neither can drift from the other.

Completion lines are TAG<TAB>VALUE<TAB>DESCRIPTION, or a directive: @dirs and
@files (complete paths), @nospace (the value continues, as KEY=), @message<TAB>TEXT
(what is expected where nothing can be listed)."""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import treeinfo  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ARCHES = ("arm64", "riscv64", "x86_64")
TREE_NOTE = ("TREE is a tree's ID or a unique prefix of 4 or more of its hex digits, or a path "
             "inside its source; omitted, the current tree.")
ID_NOTE = "TREE is a tree's ID or a unique prefix of it; a path is not accepted here."


class Opt:
    def __init__(self, names, desc, value=None, kind=None, repeat=False, optional=False, when=None):
        self.names = names.split("|")
        self.desc = desc
        self.value = value
        self.kind = kind
        self.repeat = repeat
        self.optional = optional
        self.when = when

    def spec(self):
        s = "|".join(self.names)
        if self.value:
            s += " [%s]" % self.value if self.optional else " " + self.value
        return s


class Cmd:
    def __init__(self, name, summary, usage, args=(), opts=(), notes=(), own_help=False,
                 aliases=(), rest=None):
        self.name = name
        self.summary = summary
        self.usage = usage
        self.args = list(args)
        self.opts = list(opts)
        self.notes = list(notes)
        self.own_help = own_help
        self.aliases = list(aliases)
        self.rest = rest

    def opt(self, word):
        return next((o for o in self.opts if word in o.names), None)


COMMANDS = [
    Cmd("trees", "every registered tree: kernel, commit, build state, guests, source",
        "[ls] [-q|--json|--narrow]", aliases=["ls"], opts=[
            Opt("-q", "IDs only, one per line"),
            Opt("--json", "everything known about each tree, as JSON"),
            Opt("--narrow", "one block per tree instead of a table row, for a terminal "
                            "narrower than the table")]),
    Cmd("inspect", "everything known about one tree", "[TREE] [--json|--format KEY]",
        args=["tree"], notes=[TREE_NOTE], opts=[
            Opt("--json", "everything known, as JSON"),
            Opt("--format|-f", "print one field (a JSON key or a tree.conf key) and nothing else",
                "KEY", "fkey")]),
    Cmd("add", "register a kernel source you already have, under a new ID",
        "DIR [--arch A] [--version V] [--upstream U]",
        args=["dir"], opts=[
            Opt("--arch", "arm64, riscv64 or x86_64 (default: read from DIR/.config)", "ARCH", "arch"),
            Opt("--version", "the tag or branch sync follows (default: what is checked out)",
                "VERSION", "text:a tag or branch"),
            Opt("--upstream", "where sync fetches from: stable (default) or mainline",
                "UPSTREAM", "choice:stable,mainline")]),
    Cmd("init", "register a new tree under a new ID, to clone into DIR with sync; recreate "
                "recorded trees; or record the workspace",
        "DIR --arch ARCH [--version V] [--upstream U] | --restore [ID...] | --workspace DIR",
        args=["initarg*"], opts=[
            Opt("--arch", "arm64, riscv64 or x86_64", "ARCH", "arch"),
            Opt("--version", "the tag or branch to clone and follow", "VERSION",
                "text:a tag or branch"),
            Opt("--upstream", "where sync fetches from: stable (default) or mainline",
                "UPSTREAM", "choice:stable,mainline"),
            Opt("--restore", "recreate the recorded workspace and the recorded trees (all, or "
                             "the IDs given), each under the ID it was recorded with"),
            Opt("--workspace", "record the workspace: where the rootfs and firmware are built "
                               "and what the lab/ record spells as @KBL_WORKSPACE@", "DIR", "dir")],
        notes=["DIR is required: the kernel source goes exactly there, and the tree is known "
               "by its ID from then on."]),
    Cmd("get", "read a tree's tree.conf, all of it or some keys", "TREE [KEY...]",
        args=["tree", "key*"], notes=[TREE_NOTE]),
    Cmd("set", "write keys into a tree's tree.conf", "TREE KEY=VALUE...",
        args=["tree", "kv*"], notes=[TREE_NOTE, "SRC is stored as an absolute path; ~ is expanded."]),
    Cmd("unset", "remove keys from a tree's tree.conf", "TREE KEY...",
        args=["tree", "key*"], notes=[TREE_NOTE, "SRC and ARCH cannot be removed."]),
    Cmd("edit", "open a tree's tree.conf in $VISUAL or $EDITOR", "[TREE]", args=["tree"],
        notes=[TREE_NOTE]),
    Cmd("mv", "move a tree's source, or only repoint SRC", "ID DIR [--no-move]",
        args=["treeid", "dir"], notes=[ID_NOTE], opts=[
            Opt("--no-move|--src-only", "do not move anything: point SRC at DIR, where you "
                                        "moved the source yourself")]),
    Cmd("rm", "unregister a tree", "ID [--purge] [--yes]", args=["treeid"],
        notes=[ID_NOTE], opts=[
            Opt("--purge", "also delete its source, its data and its indexes (asks first)"),
            Opt("--yes|-y", "do not ask")]),
    Cmd("prune", "drop trees whose source is gone and data no tree owns", "[--yes]", opts=[
        Opt("--yes|-y", "do not ask")]),
    Cmd("update", "one pass: check, sync, keep .config, incremental build, modules, indexes, "
                  "then a summary", "[TREE...|--all] [--no-tags] [--no-clangd] [--no-warm] [--fresh]",
        args=["tree*"], notes=[TREE_NOTE], opts=[
            Opt("--all", "every registered tree"),
            Opt("--no-tags", "skip ctags, cscope, gtags and clangd"),
            Opt("--no-clangd", "skip the clangd database and index"),
            Opt("--no-warm", "write the clangd database and config, leave the index cold"),
            Opt("--fresh", "rebuild every index from scratch, reusing no cached state")]),
    Cmd("sync", "fetch, and fast-forward to the newest patch level of the tracked version",
        "[TREE]", args=["tree"], notes=[TREE_NOTE]),
    Cmd("config", "make or keep .config (the recorded one, the existing one, or defconfig) "
                  "and merge the tree's config-fragment",
        "[TREE] [--preset]", args=["tree"], notes=[TREE_NOTE], opts=[
            Opt("--preset", "apply the debuggability preset; its soft half adds simulated and "
                            "emulated devices and their tracing without moving an existing answer")]),
    Cmd("build", "build in the container, then the tree's EXT_MODULES", "[TREE] [--ext-only] [-- MAKE ARGS]",
        args=["tree"], notes=[TREE_NOTE], rest="MAKE ARGS", opts=[
            Opt("--ext-only", "build only the tree's EXT_MODULES, against its last build")]),
    Cmd("tags", "refresh ctags, cscope, gtags and the clangd index",
        "[TREE] [--no-clangd|--clangd-only] [--no-warm] [--fresh]", args=["tree"], notes=[TREE_NOTE],
        opts=[
            Opt("--no-clangd", "only ctags, cscope and gtags"),
            Opt("--clangd-only", "only the clangd database, config and index"),
            Opt("--no-warm", "leave the clangd index cold"),
            Opt("--fresh", "rebuild every index from scratch, reusing no cached state")]),
    Cmd("image", "build the container image", "[legacy]",
        args=["choice:legacy"], notes=["legacy: Ubuntu 24.04 with gcc-9 through gcc-13 (aarch64 cross "
                                       "only), for a tree that names it with IMAGE= in its tree.conf."]),
    Cmd("run", "boot a built tree under QEMU, frozen at reset for a debugger",
        "[TREE] [options] [-- QEMU ARGS]", args=["tree"], own_help=True, rest="QEMU ARGS", opts=[
            Opt("--run", "boot now instead of freezing at reset"),
            Opt("--boot", "direct, uboot or uefi (default: the tree's BOOT)", "MODE",
                "choice:direct,uboot,uefi"),
            Opt("--board", "boot on a real SoC's machine model from boards/ instead of the tree's "
                           "machine; alone, list the boards for the tree's arch", "NAME", "board",
                optional=True),
            Opt("--kaslr", "boot with KASLR active"),
            Opt("--no-kaslr|--nokaslr", "deterministic base (the default unless the tree's KASLR says otherwise)"),
            Opt("--kvm", "x86 only: KVM, faster and less deterministic"),
            Opt("--no-kvm", "TCG, deterministic early-boot hardware breakpoints (the default)"),
            Opt("--initrd", "root fs image", "PATH", "file"),
            Opt("--no-initrd", "boot without a root fs (early-boot only)"),
            Opt("--root", "initramfs, or disk: the tree's GPT system disk (default: the tree's ROOT)",
                "KIND", "choice:initramfs,disk"),
            Opt("--reset-sysdisk", "delete the system disk and make it again from ROOTFS_IMAGE"),
            Opt("--append", "extra kernel command-line words, repeatable", "WORDS",
                "text:kernel command-line words", repeat=True),
            Opt("--net", "user-mode virtio NIC with an ssh forward"),
            Opt("--no-net", "no NIC"),
            Opt("--ssh-port", "host port forwarded to the guest's :22", "N", "number:a host port"),
            Opt("--persist", "attach the tree's writable disk"),
            Opt("--no-persist", "boot without it"),
            Opt("--emulate", "attach the emulated controllers the soft presets build drivers for"),
            Opt("--no-emulate", "without them"),
            Opt("--pcie-device", "a qemu -device spec behind a PCIe root port of its own, repeatable",
                "SPEC", "text:a qemu -device spec, e.g. edu", repeat=True),
            Opt("--iommu", "DMA through the machine's IOMMU"),
            Opt("--no-iommu", "without one (the default unless the tree's IOMMU says otherwise)"),
            Opt("--modules", "append the staged modules to the initramfs (the boot partition with ROOT=disk)"),
            Opt("--no-modules", "without them"),
            Opt("--port|-g", "gdb port (default: the tree's GDB_PORT)", "N", "number:a gdb port"),
            Opt("--qmp-port", "QMP port (default: the tree's QMP_PORT)", "N", "number:a QMP port"),
            Opt("--qmp", "a QMP monitor and the vmcoreinfo device (the default)"),
            Opt("--no-qmp", "no QMP monitor and no vmcoreinfo device"),
            Opt("--mem|-m", "guest memory, as qemu's -m takes it", "SIZE", "text:a size, e.g. 2G"),
            Opt("--smp", "guest CPUs", "N", "number:a CPU count"),
            Opt("--bios", "firmware image", "PATH", "file")]),
    Cmd("attach", "attach gdb to a running guest, stopped at head.S _text by default",
        "[TREE] [options] [-- GDB ARGS]", args=["tree"], own_help=True, rest="GDB ARGS", opts=[
            Opt("--no-bootbreak|--raw", "attach without running to _text (an already-booted guest)"),
            Opt("--bootbreak", "run to _text first (the default)"),
            Opt("--stop", "where the attach stops: text, firmware or start_kernel", "WHERE",
                "choice:text,firmware,start_kernel"),
            Opt("--port|-p", "pick the guest by its gdb port", "N", "port"),
            Opt("--list|-l", "list live guests and exit"),
            Opt("--first", "with several matches, take the lowest port instead of asking"),
            Opt("--json", "print the session as JSON instead of starting gdb")]),
    Cmd("drgn", "attach drgn to a running guest over QMP", "[TREE] [options] [-- DRGN ARGS]",
        args=["tree"], own_help=True, rest="DRGN ARGS", opts=[
            Opt("--port|-p", "pick the guest by its gdb port", "N", "port"),
            Opt("--qmp-port", "pick it by its QMP port", "N", "qmpport"),
            Opt("--tcp", "connect over the QMP TCP port rather than the unix socket"),
            Opt("--wait", "wait for the guest to publish its vmcoreinfo note (default 300s)",
                "SECONDS", "number:seconds", optional=True),
            Opt("--no-symbols", "do not pass the tree's vmlinux to drgn"),
            Opt("--list|-l", "list live guests and exit"),
            Opt("--first", "with several matches, take the lowest port instead of asking")]),
    Cmd("resume", "resume a guest a dead debugger left paused", "[TREE] [options]",
        args=["tree"], own_help=True, opts=[
            Opt("--port|-p", "pick the guest by its gdb port", "N", "port"),
            Opt("--list|-l", "list live guests and exit"),
            Opt("--first", "with several matches, take the lowest port instead of asking"),
            Opt("--force", "resume even while a debugger is attached")]),
    Cmd("doctor", "what is installed, and what is missing", ""),
    Cmd("containers", "what the container runtime holds for kbuildlab, or remove it",
        "[status | clean [--images] [--dangling]]", args=["choice:status,clean"], opts=[
            Opt("--images", "also remove the images kbuildlab built (kbuildlab:*, kbuildlab-*:*); "
                            "an image a tree names but kbuildlab did not build is kept", when="clean"),
            Opt("--dangling", "also remove every untagged image on this host, not only "
                              "kbuildlab's", when="clean")],
        notes=["status only reports.  clean removes the stopped containers made from "
               "kbuildlab's images and leaves running ones alone."]),
    Cmd("help", "this list, or one command's options", "[COMMAND]", args=["cmd"]),
]
BY_NAME = {n: c for c in COMMANDS for n in [c.name] + c.aliases}


def _wrap(text, width, indent):
    words, lines, line = text.split(), [], ""
    for w in words:
        if line and len(line) + 1 + len(w) > width - indent:
            lines.append(line)
            line = w
        else:
            line = (line + " " + w) if line else w
    if line:
        lines.append(line)
    return lines or [""]


def render_help(cmd):
    out = ["kbuildlab %s%s" % (cmd.name, (" " + cmd.usage) if cmd.usage else "")]
    out.extend("  " + b for b in _wrap(cmd.summary, 80, 2))
    if cmd.aliases:
        out.append("  (also: %s)" % ", ".join("kbuildlab " + a for a in cmd.aliases))
    rows = [(o.spec(), (("%s only: " % o.when) if o.when else "") + o.desc) for o in cmd.opts]
    if cmd.rest:
        rows.append(("-- " + cmd.rest, "passed straight to " + cmd.rest.split()[0].lower()))
    if rows:
        out.append("")
        col = min(max(len(spec) for spec, _ in rows) + 2, 26)
        for spec, desc in rows:
            body = _wrap(desc, 80, 2 + col)
            if len(spec) + 2 > col:
                out.append("  " + spec)
                out.extend("  " + " " * col + b for b in body)
            else:
                out.append("  " + spec.ljust(col) + body[0])
                out.extend("  " + " " * col + b for b in body[1:])
    if cmd.notes:
        out.append("")
        for n in cmd.notes:
            out.extend("  " + b for b in _wrap(n, 80, 2))
    return "\n".join(out)


def cmd_help(args):
    if not args:
        print("usage: cli.py help COMMAND", file=sys.stderr)
        return 2
    c = BY_NAME.get(args[0])
    if c is None:
        print("kbuildlab: no command '%s' (kbuildlab help lists them)" % args[0], file=sys.stderr)
        return 1
    print(render_help(c))
    return 0


# ----------------------------------------------------------------------------- completion

def _emit(tag, value, desc=""):
    print("%s\t%s\t%s" % (tag, value, " ".join(str(desc).split())))


def _tree_dir(word):
    """The registered tree a TREE word names, or None (paths are not resolved:
    completing a path argument never needs the tree it lies in)."""
    tid = treeinfo.resolve(word)
    return os.path.join(treeinfo.TREES, tid) if tid else None


def _conf_keys(tdir):
    return treeinfo.conf_keys(tdir) if tdir and os.path.isdir(tdir) else []


def _code_keys():
    """Every tree.conf key kbuildlab reads, from the code that reads it."""
    keys, pat = set(), re.compile(r'(?:kbl_tree_get\s+"[^"]*"\s+|conf_get\([^,()]+,\s*")([A-Z][A-Z0-9_]+)')
    for d in ("lib", "lib/index", "bin"):
        base = os.path.join(REPO, d)
        for f in sorted(os.listdir(base)) if os.path.isdir(base) else ():
            p = os.path.join(base, f)
            if os.path.isfile(p):
                try:
                    with open(p, encoding="utf-8", errors="replace") as fh:
                        keys.update(pat.findall(fh.read()))
                except OSError:
                    pass
    for t in treeinfo.ids():
        keys.update(k for k in _conf_keys(os.path.join(treeinfo.TREES, t)) if k.startswith("GDBTOOLS_"))
    return keys


def _live():
    rows = []
    for tdir, gs in treeinfo.guests().items():
        for g in gs:
            rows.append((os.path.basename(tdir) or "?", g))
    return rows


def _values(kind, pos, cur):
    """Candidates for one argument of the given kind."""
    if kind.startswith("choice:"):
        for v in kind.split(":", 1)[1].split(","):
            _emit("value", v)
    elif kind in ("tree", "treeid"):
        live = {os.path.realpath(t): len(g) for t, g in treeinfo.guests().items()}
        every = treeinfo.ids()
        for t in every:
            b = treeinfo.basic(t, every)
            g = live.get(os.path.realpath(b["config"]))
            _emit("tree", b["label"], "%s %s %s%s" % (
                b["arch"] or "?", b["ref"] or "", b["src"] or "",
                ("  %d guest%s" % (g, "s" if g > 1 else "")) if g else ""))
        if kind == "tree" and (cur.startswith(("/", ".", "~")) or not every):
            print("@dirs")
    elif kind == "dir":
        print("@dirs")
    elif kind == "file":
        print("@files")
    elif kind == "arch":
        for a in ARCHES:
            _emit("value", a)
    elif kind == "board":
        d = os.path.join(REPO, "boards")
        for f in sorted(os.listdir(d)) if os.path.isdir(d) else ():
            if f.endswith(".conf"):
                p = os.path.join(d, f)
                _emit("value", f[:-5], "%s %s" % (treeinfo.conf_file_get(p, "ARCH") or "?",
                                                  treeinfo.conf_file_get(p, "MACHINE") or ""))
    elif kind == "labid":
        lab = os.path.join(REPO, "lab", "trees")
        have = set(treeinfo.ids())
        rec = sorted(d for d in os.listdir(lab) if d not in have and re.fullmatch(r"[0-9a-f]{16}", d)
                     and os.path.isfile(os.path.join(lab, d, "tree.conf"))) if os.path.isdir(lab) else []
        for d in rec:
            b = treeinfo.conf_get(os.path.join(lab, d), "ARCH") or "?"
            _emit("value", d, "recorded in lab/trees: %s %s" % (b, treeinfo.conf_get(os.path.join(lab, d), "VERSION") or ""))
    elif kind in ("key", "kv"):
        tdir = _tree_dir(pos[0]) if pos else None
        have = _conf_keys(tdir)
        keys = sorted(set(have) | (_code_keys() if kind == "kv" else set()))
        for k in keys:
            _emit("key", k + ("=" if kind == "kv" else ""), "set" if k in have else "")
        if kind == "kv":
            print("@nospace")
    elif kind == "fkey":
        tdir = _tree_dir(pos[0]) if pos else None
        tid = os.path.basename(tdir) if tdir else (treeinfo.ids() or [None])[0]
        keys = set(treeinfo.info(tid, {}).keys()) - {"problems", "guests"} if tid else set()
        for k in sorted(keys | set(_conf_keys(tdir))):
            _emit("key", k)
    elif kind in ("port", "qmpport"):
        rows = _live()
        for tree, g in rows:
            port = g.get("port") if kind == "port" else g.get("qmp")
            if port:
                _emit("value", port, "%s, %s%s" % (tree, g.get("boot") or "?",
                                                   ", frozen" if g.get("frozen") else ""))
        if not rows:
            print("@message\tno live guest (kbuildlab run starts one)")
    elif kind == "cmd":
        for c in COMMANDS:
            _emit("command", c.name, c.summary)
    elif ":" in kind:
        print("@message\t%s" % kind.split(":", 1)[1])


def _slot(cmd, n):
    """The kind of the n-th positional argument, or None past the last."""
    if n < len(cmd.args):
        return cmd.args[n].rstrip("*")
    if cmd.args and cmd.args[-1].endswith("*"):
        return cmd.args[-1].rstrip("*")
    return None


def cmd_complete(words):
    if not words:
        words = [""]
    cur, prior = words[-1], words[:-1]
    if not prior:
        for c in COMMANDS:
            _emit("command", c.name, c.summary)
            for a in c.aliases:
                _emit("command", a, "same as %s" % c.name)
        return 0
    cmd = BY_NAME.get(prior[0])
    if cmd is None:
        return 0
    rest = prior[1:]
    if "--" in rest:
        print("@files")
        return 0
    pos, used, i, pending = [], set(), 0, None
    while i < len(rest):
        w = rest[i]
        o = cmd.opt(w)
        if o is not None:
            used.update(o.names)
            if o.value:
                if i + 1 == len(rest):
                    pending = o
                    break
                if not o.optional or rest[i + 1].isdigit() or (
                        o.kind == "board" and not rest[i + 1].startswith("-")):
                    i += 1
        elif not w.startswith("-"):
            pos.append(w)
        i += 1
    if pending is not None:
        _values(pending.kind or "", pos, cur)
        if pending.optional:
            _list_opts(cmd, used, pos)
        return 0
    if not cur.startswith("-"):
        kind = _slot(cmd, len(pos))
        if kind == "initarg":
            kind = "labid" if "--restore" in used else ("dir" if not pos else None)
        if kind:
            _values(kind, pos, cur)
    _list_opts(cmd, used, pos)
    if cmd.rest:
        _emit("option", "--", "the rest is passed straight to " + cmd.rest.split()[0].lower())
    return 0


def _list_opts(cmd, used, pos):
    for o in cmd.opts:
        if o.when and o.when not in pos:
            continue
        if used.intersection(o.names) and not o.repeat:
            continue
        for n in o.names:
            _emit("option", n, o.desc)


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0
    if argv[0] == "help":
        return cmd_help(argv[1:])
    if argv[0] == "complete":
        try:
            return cmd_complete(argv[1:])
        except Exception:  # a completion never shows a traceback mid-line
            return 0
    print("cli.py: unknown action '%s'" % argv[0], file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
