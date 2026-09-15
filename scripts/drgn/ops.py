"""Resolve the function pointers in a kernel handler table to implementations.

    kbuildlab drgn <tree> -- scripts/drgn/ops.py ext4_file_operations
    kbuildlab drgn <tree> -- scripts/drgn/ops.py --grep '_ops$'
    kbuildlab drgn <tree> -- scripts/drgn/ops.py --grep file_operations --limit 5

A `*_ops` table is a struct of function pointers, and the question asked of one
is always the same: which implementation is in this slot.  drgn answers it from
the guest's own memory plus the tree's DWARF, so the answer is what this kernel
actually holds, not what the source suggests it might.

An address outside any symbol is printed as an address.  A slot that is NULL is
printed as NULL: in a kernel that is a decision, not an absence.
"""

import argparse
import re
import sys

import drgn




def _fnptr_target(type_):
    """The function type a pointer points to, or None."""
    t = type_
    while t.kind == drgn.TypeKind.TYPEDEF:
        t = t.type
    if t.kind != drgn.TypeKind.POINTER:
        return None
    p = t.type
    while p.kind == drgn.TypeKind.TYPEDEF:
        p = p.type
    return p if p.kind == drgn.TypeKind.FUNCTION else None


def describe(prog, addr):
    """symbol + source location for a code address."""
    if addr == 0:
        return "NULL"
    try:
        sym = prog.symbol(addr)
        off = addr - sym.address
        name = sym.name + (f"+{off:#x}" if off else "")
    except LookupError:
        name = f"{addr:#x}"
    try:
        loc = str(prog.source_location(addr)).strip()
        # A single frame renders as "name at file:line"; keep only the location.
        loc = loc.split(" at ", 1)[1] if " at " in loc.splitlines()[0] else ""
    except Exception:
        loc = ""
    return f"{name}" + (f"   {loc}" if loc else "")


def dump(prog, name):
    try:
        obj = prog[name]
    except KeyError:
        print(f"{name}: no such object", file=sys.stderr)
        return 1
    t = obj.type_
    while t.kind == drgn.TypeKind.TYPEDEF:
        t = t.type
    if t.kind not in (drgn.TypeKind.STRUCT, drgn.TypeKind.UNION):
        print(f"{name}: {t} is not a struct", file=sys.stderr)
        return 1

    print(f"{name}  ({t.tag or t})  @ {obj.address_:#x}")
    width = max((len(m.name) for m in t.members if m.name), default=0)
    shown = 0
    for m in t.members:
        if not m.name or _fnptr_target(m.type) is None:
            continue
        shown += 1
        try:
            addr = int(getattr(obj, m.name))
        except Exception as e:
            print(f"  {m.name:<{width}}  <unreadable: {type(e).__name__}>")
            continue
        print(f"  {m.name:<{width}}  {describe(prog, addr)}")
    if not shown:
        print("  (no function pointers)")
    return 0


def main():
    ap = argparse.ArgumentParser(prog="ops.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("names", nargs="*", help="global objects to resolve")
    ap.add_argument("--grep", metavar="RE",
                    help="list matching global symbols instead of resolving")
    ap.add_argument("--limit", type=int, default=0,
                    help="with --grep, also resolve the first N matches")
    args = ap.parse_args()

    prog = drgn.get_default_prog()
    if not (prog.flags & drgn.ProgramFlags.IS_LINUX_KERNEL):
        print("ops.py: this program is not an identified Linux kernel -- the guest "
              "has not published its vmcoreinfo note yet", file=sys.stderr)
        return 1

    names = list(args.names)
    if args.grep:
        pat = re.compile(args.grep)
        found = sorted({s.name for s in prog.symbols() if pat.search(s.name)})
        print(f"{len(found)} symbol(s) matching {args.grep!r}")
        for n in found[: args.limit or 40]:
            print(f"  {n}")
        if args.limit:
            names = found[: args.limit]
        else:
            return 0

    rc = 0
    for i, n in enumerate(names):
        if i:
            print()
        rc |= dump(prog, n)
    return rc


if __name__ == "__main__":
    sys.exit(main())
