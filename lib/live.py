#!/usr/bin/env python3
"""Hold what a running guest's kernel recognises against a device tree.

    live.py [--qmp SOCKET] [--qmp-port N] --vmlinux FILE --tree BLOB
            --scratch FILE [--guest TEXT]

The kernel is read from outside, through QEMU's monitor: QEMU translates the
kernel's addresses (gva2gpa) and saves pages of its memory to the scratch FILE
(pmemsave), and drgn supplies the symbols and layouts of the vmlinux.  Nothing
runs in the guest and nothing stops it.  A monitor serves one client at a time,
so the TCP one on 127.0.0.1 is asked when the socket's is taken.

One line per finding: "+ NAME TEXT" holds, "! NAME TEXT" does not, "? NAME
TEXT" was not read, and "| TEXT" adds detail to the line before it.  Exit
status 0 when every finding holds, 1 when one does not, 2 when the kernel
could not be read at all.
"""

import argparse
import json
import os
import re
import socket
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fdt  # noqa: E402

try:
    import drgn
except ImportError:
    print("? %-12s python's drgn module is not installed, so no running kernel is read" % "running")
    sys.exit(2)

PAGE = 4096
TRIES = 5
PAUSE = 0.05
GREETING_TIMEOUT = 3.0
REPLY_TIMEOUT = 10.0
WALK_LIMIT = 4096
TREE_LIMIT = 4 << 20
SHOWN = 8
RAM = b"System RAM"
NOTE_OWNER = b"GNU\0"


class Unread(Exception):
    pass


class Monitor:
    def __init__(self, endpoints, scratch):
        self.scratch = scratch
        self.pages = {}
        why = "none is recorded"
        for endpoint in endpoints:
            unix = isinstance(endpoint, str)
            self.sock = socket.socket(socket.AF_UNIX if unix else socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(GREETING_TIMEOUT)
            try:
                self.sock.connect(endpoint)
                self.stream = self.sock.makefile("rb")
                json.loads(self.stream.readline())
            except TimeoutError:
                why = "each serves one client at a time, and another holds it (kbuildlab drgn?)"
            except (OSError, ValueError) as e:
                why = str(e)
            else:
                self.sock.settimeout(REPLY_TIMEOUT)
                self.ask("qmp_capabilities")
                return
            self.sock.close()
        raise Unread("its monitor does not answer: %s" % why)

    def close(self):
        self.sock.close()
        try:
            os.unlink(self.scratch)
        except OSError:
            pass

    def ask(self, name, **arguments):
        message = {"execute": name}
        if arguments:
            message["arguments"] = arguments
        self.sock.sendall(json.dumps(message).encode() + b"\n")
        for line in self.stream:
            reply = json.loads(line)
            if "return" in reply:
                return reply["return"]
            if "error" in reply:
                raise OSError(reply["error"].get("desc", "the monitor refused %s" % name))
        raise OSError("the monitor closed the connection")

    def page(self, address):
        if address in self.pages:
            return self.pages[address]
        why = "no answer"
        for attempt in range(TRIES):
            if attempt:
                time.sleep(PAUSE)
            text = self.ask("human-monitor-command", **{"command-line": "gva2gpa %#x" % address})
            found = re.search(r"gpa:\s*(0x[0-9a-fA-F]+)", text)
            if not found:
                why = text.strip() or why
                continue
            try:
                self.ask("pmemsave", val=int(found.group(1), 16), size=PAGE, filename=self.scratch)
                with open(self.scratch, "rb") as f:
                    data = f.read()
            except OSError as e:
                why = str(e)
                continue
            if len(data) == PAGE:
                self.pages[address] = data
                return data
            why = "a short read"
        raise drgn.FaultError("qemu does not read %#x of the kernel's memory (%s)" % (address, why), address)

    def read(self, address, count, offset, physical):
        if physical:
            raise drgn.FaultError("physical memory is not read here", address)
        out = b""
        while count:
            base = address & ~(PAGE - 1)
            n = min(count, PAGE - (address - base))
            out += self.page(base)[address - base : address - base + n]
            address += n
            count -= n
        return out


def ones(mask):
    words = mask.bits
    return sum(bin(int(words[i])).count("1") for i in range(len(words)))


def system_ram(prog):
    out, node = [], prog["iomem_resource"].child
    for _ in range(WALK_LIMIT):
        if not node.value_():
            return fdt.merged(out)
        if node.name.string_() == RAM:
            out.append((int(node.start), int(node.end) - int(node.start) + 1))
        node = node.sibling
    raise Unread("the kernel's list of resources does not end")


def memblock(prog):
    block = prog["memblock"]
    if prog.symbol("__init_begin").address <= block.address_ < prog.symbol("__init_end").address:
        return None
    nomap = int(prog.constant("MEMBLOCK_NOMAP"))

    def regions(kind):
        return [
            (int(kind.regions[i].base), int(kind.regions[i].size), int(kind.regions[i].flags))
            for i in range(int(kind.cnt))
        ]

    mapped = fdt.merged((b, s) for b, s, flags in regions(block.memory) if not flags & nomap)
    return mapped, fdt.merged((b, s) for b, s, _ in regions(block.reserved))


def held(prog):
    at = prog["initial_boot_params"].value_()
    if not at:
        raise Unread("the kernel holds no device tree")
    magic, total = struct.unpack(">II", prog.read(at, 8))
    if magic != fdt.MAGIC or not fdt.HEADER < total <= TREE_LIMIT:
        raise Unread("what the kernel holds at %#x is not a device tree" % at)
    return fdt.parse(prog.read(at, total))


def spans(ranges):
    return ", ".join(fdt.span(*r) for r in ranges) or "none"


def total(ranges):
    return sum(size for _, size in ranges)


def exact(n):
    for unit, shift in (("GiB", 30), ("MiB", 20), ("KiB", 10)):
        if n and n % (1 << shift) == 0:
            return "%d %s" % (n >> shift, unit)
    return "%d bytes" % n


class Findings:
    def __init__(self):
        self.failed = 0

    def line(self, mark, name, text, detail=()):
        if mark == "!":
            self.failed += 1
        print("%s %-12s %s" % (mark, name, text))
        for d in list(detail)[:SHOWN]:
            print("| " + d)

    def find(self, name, check):
        try:
            holds, text, detail = check()
        except (Unread, drgn.FaultError, LookupError, fdt.FdtError) as e:
            self.line("?", name, "not read: %s" % e)
            return
        self.line("+" if holds else "!", name, text, detail)


def identify(prog, module, monitor, vmlinux):
    if not module.build_id:
        raise Unread("%s carries no build ID to know its kernel by" % vmlinux)
    try:
        start, stop = prog.symbol("__start_notes").address, prog.symbol("__stop_notes").address
        notes = prog.read(start, stop - start)
    except (drgn.FaultError, LookupError):
        notes = b""
    if module.build_id not in notes:
        if NOTE_OWNER in notes:
            raise Unread(
                "it runs another build than %s, which was built again since or belongs to another tree" % vmlinux
            )
        status = monitor.ask("query-status").get("status", "unknown")
        raise Unread("no kernel is up where %s was linked, and qemu reports the guest %s" % (vmlinux, status))
    if int(prog["system_state"]) == int(prog.constant("SYSTEM_BOOTING")):
        raise Unread("its kernel is still booting, and what it recognises is read once it is up")
    return module.build_id.hex()


def check(args, out):
    tree = fdt.load(args.tree)
    endpoints = [args.qmp] if args.qmp else []
    if args.qmp_port:
        endpoints.append(("127.0.0.1", args.qmp_port))
    monitor = Monitor(endpoints, args.scratch)
    try:
        prog = drgn.Program()
        made = prog.extra_module("vmlinux", create=True)
        module = made[0] if isinstance(made, tuple) else made
        module.try_file(args.vmlinux, force=True)
        prog.add_memory_segment(0, 0xFFFFFFFFFFFFFFFF, monitor.read, False)
        build = identify(prog, module, monitor, args.vmlinux)
        out.line("+", "running", "%s runs the kernel of %s (build %s)" % (args.guest, args.vmlinux, build[:12]))

        def its_tree():
            lost = fdt.carried(tree, held(prog))
            if not lost:
                return True, "the kernel holds %s whole" % args.tree, ()
            return False, "the kernel holds another tree than %s, in %d place(s):" % (args.tree, len(lost)), lost

        def its_cpus():
            stated, runs = fdt.cpus(tree), len(monitor.ask("query-cpus-fast"))
            known, online = ones(prog["__cpu_possible_mask"]), ones(prog["__cpu_online_mask"])
            text = "the tree states %d, qemu runs %d, the kernel knows %d and has %d online" % (
                stated,
                runs,
                known,
                online,
            )
            return stated == runs == known, text, ()

        def its_memory():
            stated = fdt.memory(tree)
            unmapped, _, placed = fdt.aside(tree)
            sized = monitor.ask("query-memory-size-summary")
            has = int(sized.get("base-memory", 0)) + int(sized.get("plugged-memory") or 0)
            ram = system_ram(prog)
            want = fdt.without(stated, unmapped)
            left = sum(n for n, nomap in placed if nomap)
            if left:
                mapped = all(fdt.inside(b, s, want) for b, s in ram) and total(ram) == total(want) - left
            else:
                mapped = ram == want
            holds = mapped and has == total(stated)
            size = fdt.human if holds else exact
            text = "the tree states %s, qemu has %s, and the kernel's System RAM is " % (size(total(stated)), size(has))
            if holds:
                rest = total(unmapped) + left
                return (
                    True,
                    text + ("that without the %s the tree leaves unmapped" % size(rest) if rest else "all of it"),
                    (),
                )
            return (
                False,
                text + size(total(ram)) + ":",
                ("it holds  %s" % spans(ram), "the tree leaves it  %s" % spans(want)),
            )

        def its_regions():
            unmapped, kept, placed = fdt.aside(tree)
            blocks = memblock(prog)
            if blocks is None:
                raise Unread("this kernel drops its memblock once it is up (no ARCH_KEEP_MEMBLOCK)")
            mapped, reserved = blocks
            lost = [
                "%s is mapped, and the tree leaves it unmapped" % fdt.span(b, s)
                for b, s in unmapped
                if any(fdt.overlap((b, s), m) for m in mapped)
            ]
            lost += [
                "%s is not reserved, and the tree reserves it" % fdt.span(b, s)
                for b, s in kept
                if not fdt.inside(b, s, reserved)
            ]
            text = "the kernel's memblock has %d region(s) unmapped and %d reserved as the tree sets them aside" % (
                len(unmapped),
                len(kept),
            )
            if placed:
                text += "; %d more are the kernel's to place" % len(placed)
            if lost:
                return False, "the kernel's memblock does not set aside what the tree does:", lost
            return True, text, ()

        out.find("live tree", its_tree)
        out.find("live cpus", its_cpus)
        out.find("live memory", its_memory)
        if any(fdt.aside(tree)):
            out.find("live aside", its_regions)
    finally:
        monitor.close()


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--qmp")
    ap.add_argument("--qmp-port", type=int)
    ap.add_argument("--vmlinux", required=True)
    ap.add_argument("--tree", required=True)
    ap.add_argument("--scratch", required=True)
    ap.add_argument("--guest", default="the guest")
    args = ap.parse_args(argv)
    out = Findings()
    try:
        check(args, out)
    except (Unread, OSError, ValueError, drgn.FaultError) as e:
        out.line("?", "running", "%s is not read: %s" % (args.guest, e))
        return 2
    return 1 if out.failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
