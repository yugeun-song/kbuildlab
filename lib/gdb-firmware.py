import struct

import gdb

MASK = (1 << 64) - 1


class _Firmware:
    path = None
    spans = []
    offsets = []


def _spans(path):
    with open(path, "rb") as f:
        head = f.read(64)
        if head[:4] != b"\x7fELF":
            raise gdb.GdbError("%s is not an ELF file" % path)
        wide, order = head[4] == 2, "<" if head[5] == 1 else ">"
        if wide:
            phoff, = struct.unpack_from(order + "Q", head, 32)
            phentsize, phnum = struct.unpack_from(order + "HH", head, 54)
        else:
            phoff, = struct.unpack_from(order + "I", head, 28)
            phentsize, phnum = struct.unpack_from(order + "HH", head, 42)
        out = []
        for i in range(phnum):
            f.seek(phoff + i * phentsize)
            ph = f.read(phentsize)
            if struct.unpack_from(order + "I", ph, 0)[0] != 1:
                continue
            if wide:
                vaddr, = struct.unpack_from(order + "Q", ph, 16)
                memsz, = struct.unpack_from(order + "Q", ph, 40)
            else:
                vaddr, = struct.unpack_from(order + "I", ph, 8)
                memsz, = struct.unpack_from(order + "I", ph, 20)
            out.append((vaddr, memsz))
    if not out:
        raise gdb.GdbError("%s has no loadable segment" % path)
    return out


def _pc():
    return int(gdb.parse_and_eval("$pc")) & MASK


def _reloc_off():
    try:
        return int(gdb.parse_and_eval("gd->reloc_off")) & MASK
    except gdb.error:
        return None


def _offset_of(pc):
    for off in (0, _reloc_off()):
        if off is None:
            continue
        for vaddr, memsz in _Firmware.spans:
            if (vaddr + off) & MASK <= pc < ((vaddr + off) & MASK) + memsz:
                return off
    return None


def _place(offset):
    if offset in _Firmware.offsets:
        return
    gdb.execute('add-symbol-file "%s" -o %#x' % (_Firmware.path.replace("\\", "\\\\").replace('"', '\\"'), offset),
                to_string=True)
    _Firmware.offsets.append(offset)
    gdb.write("firmware symbols placed at %s: %s\n"
              % ("its link address + %#x as well" % offset if offset else "its link address", _Firmware.path))


def _follow(event=None):
    if _Firmware.path is None:
        return
    try:
        off = _offset_of(_pc())
    except gdb.error:
        return
    if off is not None:
        _place(off)


class Symbols(gdb.Command):
    """fw-symbols ELF: read the firmware's symbols from ELF at its link address, and
again where the firmware has moved itself (gd->reloc_off) once it runs there.
The first copy stays: the image it describes is still in memory."""

    def __init__(self):
        super().__init__("fw-symbols", gdb.COMMAND_FILES, gdb.COMPLETE_FILENAME)

    def invoke(self, arg, from_tty):
        path = arg.strip()
        if not path:
            if _Firmware.path is None:
                raise gdb.GdbError("fw-symbols ELF")
            gdb.write("firmware symbols: %s at its link address%s\n" % (
                _Firmware.path, "".join(" and + %#x" % o for o in _Firmware.offsets if o)))
            return
        if _Firmware.path is not None and _Firmware.path != path:
            raise gdb.GdbError("fw-symbols: already following %s" % _Firmware.path)
        _Firmware.spans = _spans(path)
        _Firmware.path = path
        _place(0)
        _follow()


class Relocated(gdb.Command):
    """fw-relocated: run the firmware until it has moved itself and stop at
board_init_r, the first function it runs there, with its symbols in place."""

    def __init__(self):
        super().__init__("fw-relocated", gdb.COMMAND_RUNNING)

    def invoke(self, arg, from_tty):
        if _Firmware.path is None:
            raise gdb.GdbError("fw-relocated: no firmware symbols (fw-symbols ELF reads them)")
        off = _reloc_off()
        if off and _offset_of(_pc()) == off:
            gdb.write("the firmware already runs at its link address + %#x\n" % off)
            return
        for name in ("relocate_code", "board_init_r", "gd"):
            try:
                gdb.parse_and_eval("&" + name if name != "gd" else "sizeof(*gd)")
            except gdb.error:
                raise gdb.GdbError("fw-relocated: %s names no %s; it does not move itself the way this follows"
                                   % (_Firmware.path, name))
        gdb.execute("thbreak relocate_code")
        gdb.execute("continue")
        off = _reloc_off()
        if not off:
            raise gdb.GdbError("fw-relocated: stopped before the move and gd->reloc_off is not set")
        entry = (int(gdb.parse_and_eval("(unsigned long)&board_init_r")) + off) & MASK
        gdb.execute("thbreak *%#x" % entry)
        gdb.execute("continue")
        _follow()


Symbols()
Relocated()
gdb.events.stop.connect(_follow)
