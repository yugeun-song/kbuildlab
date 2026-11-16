#!/usr/bin/env python3
"""Hold lib/fdt.py against libfdt's own tools and against damaged input.

    fdt.py              every check
    fdt.py --fuzz N     N damaged blobs instead of the default
    fdt.py --seed S     the damage starts from seed S (printed with a failure)

Blobs are compiled here with dtc; fdtget reads them back as the reference.
"""

import os
import random
import shutil
import struct
import subprocess
import sys
import tempfile

here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(here), "lib"))
import fdt  # noqa: E402

failed = 0
checked = 0


def check(ok, what):
    global failed, checked
    checked += 1
    if not ok:
        failed += 1
        print("FAIL " + what)


RICH = r"""
/dts-v1/;
/memreserve/ 0x0000000080000000 0x0000000000010000;
/memreserve/ 0x00000000a0000000 0x0000000000200000;
/ {
    #address-cells = <2>;
    #size-cells = <2>;
    model = "kbuildlab,test";
    compatible = "kbuildlab,test", "linux,dummy";
    interrupt-parent = <&gic>;
    empty-prop;
    bytes = [de ad be ef 01];
    mixed = "text", <0x1 0x2>;
    odd = [00 01 02];

    aliases { serial0 = "/soc/uart@9000000"; };

    chosen {
        stdout-path = "serial0:115200n8";
        bootargs = "console=ttyAMA0";
        kaslr-seed = <0x11111111 0x22222222>;
        rng-seed = <0x1 0x2 0x3 0x4>;
    };

    memory@40000000 {
        device_type = "memory";
        reg = <0x0 0x40000000 0x1 0x00000000>;
        numa-node-id = <0>;
    };

    memory@200000000 {
        device_type = "memory";
        reg = <0x2 0x00000000 0x0 0x40000000>, <0x3 0x00000000 0x0 0x10000000>;
        linux,usable-memory = <0x2 0x00000000 0x0 0x20000000>;
        status = "disabled";
    };

    reserved-memory {
        #address-cells = <2>;
        #size-cells = <2>;
        ranges;
        fw@80000000 { reg = <0x0 0x80000000 0x0 0x40000>; no-map; };
        cma { compatible = "shared-dma-pool"; reusable; size = <0x0 0x2000000>; };
    };

    cpus {
        #address-cells = <1>;
        #size-cells = <0>;
        cpu@0 { device_type = "cpu"; compatible = "arm,cortex-a710"; reg = <0>; enable-method = "psci"; };
        cpu@1 { device_type = "cpu"; compatible = "arm,cortex-a710"; reg = <1>; enable-method = "psci"; };
        cpu@2 { device_type = "cpu"; compatible = "arm,cortex-a53"; reg = <2>; enable-method = "psci"; status = "fail"; };
    };

    gic: intc@8000000 {
        compatible = "arm,gic-v3";
        interrupt-controller;
        #interrupt-cells = <3>;
        reg = <0x0 0x8000000 0x0 0x10000>;
    };

    soc {
        #address-cells = <1>;
        #size-cells = <1>;
        compatible = "simple-bus";
        ranges;
        uart@9000000 { compatible = "arm,pl011", "arm,primecell"; reg = <0x9000000 0x1000>; };
        virtio@a000000 { compatible = "virtio,mmio"; reg = <0xa000000 0x200>; };
        virtio@a000200 { compatible = "virtio,mmio"; reg = <0xa000200 0x200>; };
        virtio@a000400 { compatible = "virtio,mmio"; reg = <0xa000400 0x200>; status = "disabled"; };
        deep { a { b { c { d { leaf = <1>; }; }; }; }; };
    };
};
"""

BARE = "/dts-v1/;\n/ { };\n"

NOCELLS = r"""
/dts-v1/;
/ {
    memory@0 { device_type = "memory"; reg = <0x0 0x80000000 0x10000000>; };
};
"""

OVERLAY = r"""
/dts-v1/;
/plugin/;
&{/memory@40000000} { reg = <0x0 0x40000000 0x0 0x20000000>; };
"""


def dtc(source, out, *opts):
    r = subprocess.run(
        ["dtc", "-q", "-I", "dts", "-O", "dtb", *opts, "-o", out, "-"], input=source.encode(), capture_output=True
    )
    if r.returncode != 0:
        raise SystemExit("dtc failed on a test source: " + r.stderr.decode())
    with open(out, "rb") as f:
        return f.read()


def fdtget(blob_path, *args):
    r = subprocess.run(["fdtget", *args], capture_output=True)
    return r.returncode, r.stdout.decode(errors="replace")


def reference(blob_path, path="/"):
    out = {}
    rc, props = fdtget(blob_path, "-p", blob_path, path)
    assert rc == 0, path
    node = {}
    for name in props.split():
        rc, val = fdtget(blob_path, "-t", "bx", blob_path, path, name)
        node[name] = bytes(int(b, 16) for b in val.split()) if rc == 0 else b""
    out[path] = node
    rc, kids = fdtget(blob_path, "-l", blob_path, path)
    for kid in kids.split():
        out.update(reference(blob_path, (path.rstrip("/") + "/" + kid)))
    return out


def mine(tree):
    return {n.path: dict(n.props) for n in tree.root.walk()}


def with_nops(blob):
    data = bytearray(blob)
    off_struct, off_strings = struct.unpack_from(">II", data, 8)
    pos = off_struct
    while True:
        (token,) = struct.unpack_from(">I", data, pos)
        if token == fdt.PROP:
            length, name_off = struct.unpack_from(">II", data, pos + 4)
            end = data.index(b"\0", off_strings + name_off)
            size = 12 + ((length + 3) & ~3)
            if bytes(data[off_strings + name_off : end]) == b"kaslr-seed":
                data[pos : pos + size] = struct.pack(">I", fdt.NOP) * (size // 4)
                return bytes(data)
            pos += size
        elif token == fdt.BEGIN_NODE:
            end = data.index(b"\0", pos + 4)
            pos = off_struct + ((end + 1 - off_struct + 3) & ~3)
        elif token in (fdt.END_NODE, fdt.NOP):
            pos += 4
        else:
            raise SystemExit("no kaslr-seed in the test blob")


SMALL = r"""
/dts-v1/;
/ {
    #address-cells = <2>;
    #size-cells = <2>;
    memory@40000000 { device_type = "memory"; reg = <0x0 0x40000000 0x0 0x40000000>; };
%s
};
"""

CMA = "cma { compatible"
MEMORY = (0x40000000, 0x100000000)
FIRMWARE = (0x80000000, 0x40000)


def agreement(tmp, t, variant, made):
    check(fdt.agrees(t, t) == [], "a tree agrees with itself")
    lines = fdt.agrees(t, made["half"])
    check(
        len(lines) == 1 and lines[0].startswith("~ /memory@40000000 reg:"),
        "memory the machine does not have is not agreed to",
    )
    check(
        all(fdt.agrees(t, made[k]) == [] for k in ("seeds", "args", "grown")),
        "/chosen is the tree's own to change",
    )
    check(fdt.agrees(t, made["gone"]) == ["- /soc/uart@9000000 (node)"], "a device of the machine may not be left out")
    alias = variant('serial0 = "/soc/uart@9000000";', 'serial0 = "/soc/virtio@a000000"; second = "/soc/uart@9000000";')
    check(fdt.agrees(t, alias) == [], "/aliases is the tree's own to change")
    off = variant(
        'uart@9000000 { compatible = "arm,pl011"', 'uart@9000000 { status = "disabled"; compatible = "arm,pl011"'
    )
    check(fdt.agrees(t, off) == ['+ /soc/uart@9000000 status: "disabled"'], "a device may not be switched off")

    def reserved(text):
        return fdt.agrees(t, variant(CMA, text + "\n        " + CMA))

    def one(text, word):
        lines = reserved(text)
        return len(lines) == 1 and word in lines[0]

    check(reserved("mine@50000000 { reg = <0x0 0x50000000 0x0 0x100000>; no-map; };") == [], "memory may be set aside")
    check(
        reserved(
            'pool { compatible = "shared-dma-pool"; reusable; size = <0x0 0x400000>; alignment = <0x0 0x200000>; };'
        )
        == [],
        "and left to the kernel to place",
    )
    check(
        one("far@200000000 { reg = <0x2 0x0 0x0 0x1000>; };", "lies outside the machine's memory"),
        "not where the machine has none: a memory node that is switched off is no memory",
    )
    check(
        one("edge@13ffff000 { reg = <0x1 0x3ffff000 0x0 0x2000>; };", "lies outside the machine's memory"),
        "nor across the end of it",
    )
    check(reserved("last@13ffff000 { reg = <0x1 0x3ffff000 0x0 0x1000>; };") == [], "the last page is inside")
    check(
        one("over@80010000 { reg = <0x0 0x80010000 0x0 0x1000>; };", "overlaps"), "nor over what is set aside already"
    )
    check(
        len(
            reserved(
                "a@50000000 { reg = <0x0 0x50000000 0x0 0x2000>; };\n b@50001000 { reg = <0x0 0x50001000 0x0 0x2000>; };"
            )
        )
        == 1,
        "nor twice over the same memory",
    )
    check(
        one("none@50000000 { reg = <0x0 0x50000000 0x0 0x0>; };", "an empty range"), "an empty range is no reservation"
    )
    check(one("bare { no-map; };", "neither reg nor size"), "nor is a node without reg and size")
    check(one("cut@50000000 { reg = <0x0 0x50000000 0x0>; };", "not whole pairs"), "nor a reg cut short")
    check(one("zero { size = <0x0 0x0>; };", "nothing to set aside"), "nor a size of nothing")
    check(
        one("dyn { size = <0x0 0x1000>; alloc-ranges = <0x3 0x0 0x0 0x1000000>; };", "alloc-ranges"),
        "nor a place to allocate from that the machine lacks",
    )
    check(
        reserved('unused@200000000 { reg = <0x2 0x0 0x0 0x1000>; status = "disabled"; };') == [],
        "a node switched off sets nothing aside",
    )
    check(
        fdt.agrees(t, variant("fw@80000000 { reg = <0x0 0x80000000 0x0 0x40000>; no-map; };", ""))
        == ["- /reserved-memory/fw@80000000 (node)"],
        "what the base sets aside stays set aside",
    )
    lines = fdt.agrees(
        t, variant("reg = <0x0 0x80000000 0x0 0x40000>; no-map;", "reg = <0x0 0x80000000 0x0 0x20000>; no-map;")
    )
    check(len(lines) == 1 and lines[0].startswith("~ /reserved-memory/fw@80000000 reg:"), "and as large as it was")
    lines = fdt.agrees(
        t,
        variant(
            "/memreserve/ 0x00000000a0000000",
            "/memreserve/ 0x0000000300000000 0x1000;\n/memreserve/ 0x00000000a0000000",
        ),
    )
    check(
        len(lines) == 1 and "memreserve" in lines[0] and "lies outside" in lines[0], "a reservation outside the memory"
    )

    def small(body, name):
        return fdt.parse(dtc(SMALL % body, os.path.join(tmp, name)))

    plain = small("", "small.dtb")
    good = "reserved-memory { #address-cells = <2>; #size-cells = <2>; ranges; r@50000000 { reg = <0x0 0x50000000 0x0 0x1000>; }; };"
    check(fdt.agrees(plain, small(good, "g.dtb")) == [], "a tree may bring /reserved-memory itself")
    lines = fdt.agrees(
        plain, small(good.replace("#size-cells = <2>", "#size-cells = <1>").replace("0x0 0x1000", "0x1000"), "c.dtb")
    )
    check(any("#size-cells" in line for line in lines), "but with the root's cells, or the kernel passes over it")
    lines = fdt.agrees(plain, small(good.replace(" ranges;", ""), "r.dtb"))
    check(len(lines) == 1 and "ranges" in lines[0], "and with ranges")
    check(
        fdt.agrees(plain, small('chosen { x; }; aliases { a = "/memory@40000000"; };', "o.dtb")) == [],
        "or /chosen and /aliases",
    )

    kernel = [("kernel", 0x40200000, 0x2000000)]
    check(fdt.agrees(t, t, kernel) == [], "a kernel loaded in free memory")
    lines = fdt.agrees(t, t, [("kernel", 0x80000000, 0x100000)])
    check(
        len(lines) == 2 and all("covers the kernel" in line for line in lines),
        "a kernel loaded where memory is set aside, by a node and by a reservation",
    )
    lines = fdt.agrees(t, t, [("initramfs", 0x200000000, 0x1000)])
    check(
        len(lines) == 1 and "lies outside the machine's memory" in lines[0],
        "an initramfs loaded where there is no memory",
    )
    check(fdt.agrees(t, t, [("device tree", 0x13FFFF000, 0x1000)]) == [], "the last page takes a load")
    check(fdt.memory(t) == [MEMORY] and fdt.memory(plain) == [(0x40000000, 0x40000000)], "the machine's memory")

    rng = random.Random(20261116)
    spots = [
        0x3FFFF000,
        0x40000000,
        0x50000000,
        0x7FFFF000,
        0x80000000,
        0x8003F000,
        0x80040000,
        0x13FFFF000,
        0x140000000,
    ]
    sizes = [0, 0x1000, 0x2000, 0x40000, 0x10000000, 0x100000000]
    wrong = 0
    for case in range(300):
        regions = [(rng.choice(spots) + rng.choice([0, 0x1000]), rng.choice(sizes)) for _ in range(rng.randint(1, 3))]
        text = "\n".join(
            "n%d { reg = <%#x %#x %#x %#x>; };" % (i, b >> 32, b & 0xFFFFFFFF, s >> 32, s & 0xFFFFFFFF)
            for i, (b, s) in enumerate(regions)
        )
        refused = any(s == 0 or not (MEMORY[0] <= b and b + s <= MEMORY[0] + MEMORY[1]) for b, s in regions)
        taken = [FIRMWARE] + regions
        for i, (b, s) in enumerate(taken):
            for c, d in taken[i + 1 :]:
                refused = refused or (s and d and b < c + d and c < b + s)
        if bool(reserved(text)) != bool(refused):
            wrong += 1
            print("FAIL case %d: %s is %s" % (case, text.replace("\n", " "), "agreed to" if refused else "refused"))
    check(wrong == 0, "300 sets of regions are agreed to or refused as plain interval arithmetic says")


THIRD = (
    'cpu@2 { device_type = "cpu"; compatible = "arm,cortex-a53"; reg = <2>; enable-method = "psci"; status = "fail"; };'
)
UART = 'uart@9000000 { compatible = "arm,pl011", "arm,primecell"; reg = <0x9000000 0x1000>; };'


def moving(tmp, t, variant):
    path = os.path.join(tmp, "written.dtb")
    with open(path, "wb") as f:
        f.write(fdt.write(t))
    back = fdt.load(path)
    check(
        mine(back) == reference(path) and mine(back) == mine(t) and back.reserve == t.reserve,
        "a tree that is written reads back as it was, here and through fdtget",
    )
    check(fdt.write(back) == fdt.write(t), "and is written the same again")

    def made(text):
        return fdt.parse(dtc(text, os.path.join(tmp, "m.dtb")))

    def machine(text):
        return text.replace("gic: intc@8000000 {", "gic: intc@8000000 { phandle = <5>;").replace(THIRD, "")

    mine_ = (
        RICH.replace(THIRD, "")
        .replace("rng-seed = <0x1 0x2 0x3 0x4>;", "rng-seed = <0x1 0x2 0x3 0x4>; mine = <7>;")
        .replace(CMA, "kept@50000000 { reg = <0x0 0x50000000 0x0 0x100000>; no-map; };\n        " + CMA)
    )
    old, new = t, made(machine(RICH))
    tree, lines = fdt.rebase(old, made(mine_), new)
    check(lines == [], "a tree written on one machine goes onto another that has a CPU less and other phandles")
    check(fdt.agrees(new, tree) == [], "and then states that machine's hardware")
    check(
        tree.node("/reserved-memory/kept@50000000") is not None
        and tree.node("/chosen").cell("mine") == 7
        and tree.root.cell("interrupt-parent") == 5
        and tree.node("/cpus/cpu@2") is None,
        "with what was its own carried over, and the machine's references as the machine has them",
    )
    check(fdt.diff(fdt.parse(fdt.write(tree)), tree) == [], "and can be written")

    def problems(text, onto=new):
        return fdt.rebase(old, made(text), onto)[1]

    lines = problems(mine_.replace(UART, UART.replace("0x9000000 0x1000", "0x9100000 0x1000")))
    check(len(lines) == 1 and lines[0].startswith("~ /soc/uart@9000000 reg:"), "a device it moved is not carried")
    lines = problems(
        mine_.replace('compatible = "arm,cortex-a710"; reg = <0>', 'compatible = "arm,cortex-a53"; reg = <0>')
    )
    check(len(lines) == 1 and lines[0].startswith("~ /cpus/cpu@0 compatible:"), "nor a CPU it describes otherwise")
    lines = problems(mine_.replace("virtio@a000200 {", "virtio@a000200 { added;"))
    check(lines == ["+ /soc/virtio@a000200 added"], "nor a property it gave a device")
    lines = problems(RICH)
    check(len(lines) == 1 and "it states 3 CPUs" in lines[0], "a tree that states more CPUs than the machine has")
    lines = problems(mine_.replace("reg = <0x0 0x40000000 0x1 0x00000000>;", "reg = <0x0 0x50000000 0x1 0x00000000>;"))
    check(len(lines) == 1 and "it states memory at" in lines[0], "or memory where the machine has none")
    lines = problems(mine_.replace("fw@80000000 { reg = <0x0 0x80000000 0x0 0x40000>; no-map; };", ""))
    check(lines == ["- /reserved-memory/fw@80000000 (node)"], "or drops what the machine sets aside")
    lines = problems(mine_.replace("kept@50000000 {", "kept@50000000 { phandle = <5>;"))
    check(
        len(lines) == 1 and "carries phandle 0x5" in lines[0],
        "a node of its own may not take a phandle the machine uses",
    )
    check(
        problems(mine_.replace("mine = <7>;", "mine = <7 0x1>;")) == [],
        "a number of its own that equals a phandle of the machine is still its number",
    )
    check(
        fdt.rebase(old, old, old)[1] == [] and fdt.diff(fdt.rebase(old, old, old)[0], old) == [],
        "a tree goes onto itself",
    )


def stated(tmp, t, variant):
    check(fdt.cpus(t) == 3 and fdt.machine(t) == ["mem 4096M", "smp 3"], "the machine a tree states")
    lone = dtc(
        "/dts-v1/;\n/ { #address-cells = <2>; #size-cells = <2>;\n"
        '  memory@0 { device_type = "memory"; reg = <0 0 0 0x100000>; }; };\n',
        os.path.join(tmp, "lone.dtb"),
    )
    try:
        fdt.machine(fdt.parse(lone))
        check(False, "a tree without CPUs states no machine")
    except fdt.FdtError as e:
        check("no CPUs" in str(e), "a tree without CPUs states no machine")
    check(
        fdt.aside(t) == ([FIRMWARE], [(0x80000000, 0x10000), (0xA0000000, 0x200000)], [(0x2000000, False)]),
        "what a tree sets aside: unmapped, reserved, and left to the kernel to place",
    )
    more = variant(
        CMA,
        "a@50000000 { reg = <0x0 0x50000000 0x0 0x100000 0x0 0x50100000 0x0 0x100000>; no-map; };\n"
        "        b@60000000 { reg = <0x0 0x60000000 0x0 0x200000>; };\n"
        '        c@70000000 { reg = <0x0 0x70000000 0x0 0x100000>; no-map; status = "disabled"; };\n'
        "        pool { size = <0x0 0x400000>; no-map; };\n        " + CMA,
    )
    check(
        fdt.aside(more)
        == (
            [(0x50000000, 0x200000), FIRMWARE],
            [(0x60000000, 0x200000), (0x80000000, 0x10000), (0xA0000000, 0x200000)],
            [(0x400000, True), (0x2000000, False)],
        ),
        "ranges that touch are one, a disabled region sets nothing aside",
    )
    check(fdt.aside(fdt.parse(lone)) == ([], [], []), "a tree may set nothing aside")
    rng = random.Random(20261116)
    wrong = 0
    for _ in range(300):
        spans = [(rng.randrange(40), rng.randrange(9)) for _ in range(rng.randrange(5))]
        holes = [(rng.randrange(40), rng.randrange(9)) for _ in range(rng.randrange(5))]
        want = {x for b, n in spans for x in range(b, b + n)} - {x for b, n in holes for x in range(b, b + n)}
        got = fdt.without(spans, holes)
        cells = [x for b, n in got for x in range(b, b + n)]
        wrong += not (set(cells) == want and len(cells) == len(want) and got == fdt.merged(got))
    check(wrong == 0, "ranges without others, against a model of single cells (300 cases)")


def unit(tmp):
    rich_path = os.path.join(tmp, "rich.dtb")
    rich = dtc(RICH, rich_path)
    padded = dtc(RICH, os.path.join(tmp, "padded.dtb"), "-p", "4096")
    bare = dtc(BARE, os.path.join(tmp, "bare.dtb"))
    nocells = dtc(NOCELLS, os.path.join(tmp, "nocells.dtb"))
    overlay = dtc(OVERLAY, os.path.join(tmp, "overlay.dtbo"), "-@")

    t = fdt.parse(rich)
    check(mine(t) == reference(rich_path), "the parsed tree is what fdtget reads, property for property")
    check(t.reserve == [(0x80000000, 0x10000), (0xA0000000, 0x200000)], "memory reservations")
    check(fdt.diff(t, fdt.parse(padded)) == [], "padding changes nothing")
    check(fdt.identity(t) == fdt.identity(fdt.parse(padded)), "padding keeps the identity")
    check(mine(fdt.parse(bare)) == {"/": {}}, "an empty root")
    check(fdt.parse(rich + b"trailing junk").header["totalsize"] == len(rich), "bytes after the blob are not read")

    s = fdt.summary(t)
    check(
        s["model"] == "kbuildlab,test" and s["compatible"] == ["kbuildlab,test", "linux,dummy"], "model and compatible"
    )
    check((s["address_cells"], s["size_cells"]) == (2, 2), "root cells")
    mem = {m["path"]: m for m in s["memory"]}
    check(mem["/memory@40000000"]["reg"] == [(0x40000000, 0x100000000)], "a 4 GiB memory node")
    check(
        mem["/memory@200000000"]["reg"] == [(0x200000000, 0x40000000), (0x300000000, 0x10000000)],
        "two ranges in one reg",
    )
    check(mem["/memory@200000000"]["usable"] == [(0x200000000, 0x20000000)], "linux,usable-memory")
    check(
        mem["/memory@200000000"]["status"] == "disabled" and mem["/memory@40000000"]["numa"] == 0,
        "memory status and node id",
    )
    res = {r["path"]: r for r in s["reserved"]}
    check(
        res["/reserved-memory/fw@80000000"]["reg"] == [(0x80000000, 0x40000)]
        and res["/reserved-memory/fw@80000000"]["flags"] == ["no-map"],
        "a no-map reservation",
    )
    check(
        res["/reserved-memory/cma"]["size"] == 0x2000000
        and res["/reserved-memory/cma"]["flags"] == ["reusable"]
        and res["/reserved-memory/cma"]["compatible"] == "shared-dma-pool",
        "a reservation placed at boot",
    )
    check(
        s["cpus"]
        == {
            "count": 3,
            "models": {"arm,cortex-a710": 2, "arm,cortex-a53": 1},
            "enable_method": ["psci"],
            "disabled": 1,
        },
        "cpus",
    )
    check(
        s["chosen"]["stdout"] == "serial0:115200n8" and s["chosen"]["stdout_node"] == "/soc/uart@9000000",
        "stdout-path through an alias",
    )
    check(s["chosen"]["bootargs"] == "console=ttyAMA0", "bootargs")
    check([i["path"] for i in s["interrupt_controllers"]] == ["/intc@8000000"], "interrupt controllers")
    virt = [d for d in s["devices"] if d["compatible"] == "virtio,mmio"]
    check(
        sorted((d["count"], d["status"]) for d in virt) == [(1, "disabled"), (2, None)],
        "siblings of one kind are grouped by status",
    )
    check("/soc/virtio@a000400" in s["disabled"] and "/cpus/cpu@2" in s["disabled"], "disabled nodes")
    check(s["depth"] == 6 and s["nodes"] == len(list(t.root.walk())), "depth and node count")
    check(isinstance(fdt.show(s), str) and "4 GiB" in fdt.show(s), "the summary renders")

    n = fdt.summary(fdt.parse(nocells))
    check(
        (n["address_cells"], n["size_cells"]) == (2, 1) and n["memory"][0]["reg"] == [(0x80000000, 0x10000000)],
        "a root that states no cells reads as 2 and 1",
    )
    check(
        fdt.summary(fdt.parse(bare))["memory"] == [] and fdt.show(fdt.summary(fdt.parse(bare))),
        "an empty tree summarises",
    )

    check(
        fdt.render(b"") == "" and fdt.render(b"a\0") == '"a"' and fdt.render(b"a\0b\0") == '"a", "b"', "strings render"
    )
    check(
        fdt.render(b"\0\0\0\x01") == "<0x1>" and fdt.render(b"\x01\x02\x03") == "[01 02 03]", "cells and bytes render"
    )
    check(fdt.render(b"\0") == "[00]" and fdt.render(b"a\0\0") == "[61 00 00]", "an empty string is not text")
    check(fdt.render(struct.pack(">20I", *range(20)), 4).endswith("... 20 cells>"), "long values are cut")
    check(fdt.human(1 << 30) == "1 GiB" and fdt.human(3 << 19) == "1.5 MiB" and fdt.human(12) == "12 bytes", "sizes")

    def variant(old, new, *opts):
        assert old in RICH
        return fdt.parse(dtc(RICH.replace(old, new), os.path.join(tmp, "v.dtb"), *opts))

    half = variant("reg = <0x0 0x40000000 0x1 0x00000000>;", "reg = <0x0 0x40000000 0x0 0x80000000>;")
    d = fdt.diff(t, half)
    check(len(d) == 1 and d[0].startswith("~ /memory@40000000 reg:"), "a changed property is one line")
    check(fdt.identity(t) != fdt.identity(half), "a changed memory node changes the identity")
    seeds = variant("kaslr-seed = <0x11111111 0x22222222>;", "kaslr-seed = <0x33333333 0x44444444>;")
    check(fdt.diff(t, seeds) == [] and fdt.identity(t) == fdt.identity(seeds), "another seed is no difference")
    args = variant('bootargs = "console=ttyAMA0";', 'bootargs = "console=ttyS0";')
    check(
        len(fdt.diff(t, args)) == 1 and fdt.identity(t) == fdt.identity(args),
        "/chosen is compared and not part of the identity",
    )
    gone = variant('uart@9000000 { compatible = "arm,pl011", "arm,primecell"; reg = <0x9000000 0x1000>; };', "")
    check(fdt.diff(t, gone) == ["- /soc/uart@9000000 (node)"], "a removed node")
    check(fdt.diff(gone, t) == ["+ /soc/uart@9000000 (node)"], "an added node")
    more = variant("empty-prop;", "empty-prop; added = <7>;")
    check(
        fdt.diff(t, more) == ["+ / added: <0x7>"] and fdt.diff(more, t) == ["- / added"],
        "an added and a removed property",
    )
    rsv = variant("/memreserve/ 0x00000000a0000000 0x0000000000200000;", "")
    check(len(fdt.diff(t, rsv)) == 1 and fdt.diff(t, rsv)[0].startswith("~ memreserve"), "a changed reservation")
    check(fdt.identity(t) != fdt.identity(rsv), "a reservation is part of the identity")
    moved = variant("    aliases {", "    zz-extra { };\n    aliases {")
    check(fdt.identity(moved) != fdt.identity(t), "another node is another identity")
    swapped = fdt.parse(
        dtc(
            RICH.replace(
                '    model = "kbuildlab,test";\n    compatible = "kbuildlab,test", "linux,dummy";',
                '    compatible = "kbuildlab,test", "linux,dummy";\n    model = "kbuildlab,test";',
            ),
            os.path.join(tmp, "s.dtb"),
        )
    )
    check(
        fdt.identity(swapped) == fdt.identity(t) and fdt.diff(t, swapped) == [],
        "the order of properties is not a difference",
    )

    nopped = fdt.parse(with_nops(rich))
    check(
        "kaslr-seed" not in nopped.node("/chosen").props and fdt.diff(t, nopped) == ["- /chosen kaslr-seed"],
        "a property overwritten with NOPs is gone",
    )

    check(fdt.carried(t, t) == [], "a tree carries itself")
    check(fdt.carried(t, seeds) == [] and fdt.carried(t, nopped) == [], "a loader may replace or drop the seeds")
    check(
        fdt.carried(t, args) == ['~ /chosen bootargs: "console=ttyAMA0" -> "console=ttyS0"'],
        "a loader may not change what the tree states in /chosen",
    )
    grown = variant("rng-seed = <0x1 0x2 0x3 0x4>;", 'rng-seed = <0x1 0x2 0x3 0x4>; u-boot,version = "x"; sub { p; };')
    check(
        fdt.carried(t, grown) == [] and fdt.carried(grown, t) != [], "a loader may add to /chosen, and not take from it"
    )
    check(
        fdt.carried(t, half) != [] and fdt.carried(t, more) == ["+ / added: <0x7>"] and fdt.carried(t, gone) != [],
        "a loader may not touch anything outside /chosen",
    )
    check(
        fdt.carried(rsv, t) == [] and len(fdt.carried(t, rsv)) == 1, "a loader may add a reservation, and not drop one"
    )
    nochosen = variant("    chosen {", "    chosen-not {")
    check(
        all(
            line.startswith("- /chosen-not") or line.startswith("+ /chosen-not") or "chosen" in line
            for line in fdt.carried(nochosen, t)
        ),
        "a tree without /chosen is given one",
    )

    agreement(tmp, t, variant, {"half": half, "seeds": seeds, "args": args, "grown": grown, "gone": gone})
    check(fdt.agrees(t, more) == ["+ / added: <0x7>"], "a property the machine does not state is not agreed to")
    check(fdt.agrees(t, moved) == ["+ /zz-extra (node)"], "nor is a node it does not have")
    check(fdt.agrees(rsv, t) == [], "a reservation inside the machine's memory may be added")
    check(
        fdt.agrees(t, rsv) == ["- memreserve 0xa0000000 - 0xa01fffff (2 MiB)"],
        "a reservation the base makes may not be dropped",
    )

    moving(tmp, t, variant)
    stated(tmp, t, variant)

    check(fdt.is_overlay(fdt.parse(overlay)) and not fdt.is_overlay(t), "an overlay is told from a tree")

    img = os.path.join(tmp, "Image")
    for magic, size, want in (
        (b"ARM\x64", 0x3960000, 0x3960000),
        (b"RSC\x05", 0x2F75000, 0x2F75000),
        (b"ARM\x64", 0, None),
        (b"MZ\0\0", 0x1000, None),
    ):
        with open(img, "wb") as f:
            f.write(b"\0" * 16 + struct.pack("<Q", size) + b"\0" * 32 + magic + b"\0" * 4)
        check(fdt.image_size(img) == want, "the image size of a %r header stating %#x" % (magic, size))
    with open(img, "wb") as f:
        f.write(b"short")
    check(fdt.image_size(img) is None, "a file shorter than an image header")

    for bad, why in (
        (b"", "empty"),
        (rich[:39], "a cut header"),
        (b"\0" * 64, "no magic"),
        (rich[: len(rich) - 8], "a cut blob"),
        (rich[:8] + struct.pack(">I", 2**31) + rich[12:], "a structure offset past the end"),
    ):
        try:
            fdt.parse(bad)
            check(False, "%s is refused" % why)
        except fdt.FdtError:
            check(True, why)
    twice = bytearray(rich)
    off_struct, off_strings = struct.unpack_from(">II", twice, 8)
    at = twice.index(b"model\0", off_strings) - off_strings
    to = twice.index(b"compatible\0", off_strings) - off_strings
    hit = twice.index(struct.pack(">I", to), off_struct)
    struct.pack_into(">I", twice, hit, at)
    for bad, why in (
        (bytes(twice), "a property stated twice"),
        (dtc(RICH, os.path.join(tmp, "d.dtb")).replace(b"virtio@a000200", b"virtio@a000000"), "two nodes of one name"),
    ):
        try:
            fdt.parse(bad)
            check(False, "%s is refused" % why)
        except fdt.FdtError:
            check(True, why)
    first = fdt.parse(
        dtc(RICH.replace("    chosen {", "    chosen@1 { early; };\n    chosen {"), os.path.join(tmp, "c.dtb"))
    )
    check(
        first.node("/chosen").name == "chosen@1"
        and first.node("/chosen@1") is not None
        and first.node("/soc/uart").name == "uart@9000000"
        and first.node("/nope") is None,
        "a path finds the first node of that name, with or without its unit address, as libfdt does",
    )
    old = bytearray(rich)
    struct.pack_into(">I", old, 20, 16)
    try:
        fdt.parse(bytes(old))
        check(False, "version 16 is refused")
    except fdt.FdtError:
        check(True, "version 16")
    return [rich, padded, bare, nocells, overlay, with_nops(rich)]


def deep(levels):
    name = b"n\0\0\0"
    body = (
        struct.pack(">I", fdt.BEGIN_NODE)
        + b"\0\0\0\0"
        + (struct.pack(">I", fdt.BEGIN_NODE) + name) * levels
        + struct.pack(">I", fdt.END_NODE) * (levels + 1)
        + struct.pack(">I", fdt.END)
    )
    off_struct = fdt.HEADER + 16
    total = off_struct + len(body)
    return (
        struct.pack(">10I", fdt.MAGIC, total, off_struct, total, fdt.HEADER, 17, 16, 0, 0, len(body))
        + b"\0" * 16
        + body
    )


def exercise(data, others=()):
    try:
        tree = fdt.parse(data)
    except fdt.FdtError:
        return False
    s = fdt.summary(tree)
    fdt.show(s)
    fdt.identity(tree)
    fdt.is_overlay(tree)
    fdt.memory(tree)
    unmapped, kept, _ = fdt.aside(tree)
    assert fdt.without(fdt.memory(tree), unmapped + kept) == fdt.without(fdt.without(fdt.memory(tree), unmapped), kept)
    try:
        assert fdt.machine(tree)[1] == "smp %d" % fdt.cpus(tree)
    except fdt.FdtError:
        pass
    assert fdt.diff(tree, tree) == []
    assert fdt.carried(tree, tree) == []
    assert fdt.agrees(tree, tree) == []
    back = fdt.parse(fdt.write(tree))
    assert fdt.diff(tree, back) == [] and back.reserve == tree.reserve and mine(back) == mine(tree)
    fdt.rebase(tree, tree, tree)
    for other in others:
        fdt.agrees(other, tree, [("kernel", 0x40200000, 0x2000000), ("initramfs", 0, 1)])
        fdt.agrees(tree, other)
        fdt.rebase(other, tree, other)
        fdt.rebase(tree, other, tree)
    for node in tree.root.walk():
        for value in node.props.values():
            fdt.render(value, 4)
    return True


def damage(rng, blob):
    data = bytearray(blob)
    kind = rng.randrange(9) if len(data) >= 64 else rng.choice([0, 5])
    if kind == 0:
        for _ in range(rng.randint(1, 8)):
            data[rng.randrange(len(data))] ^= 1 << rng.randrange(8)
    elif kind == 1:
        at = rng.randrange(len(data))
        data[at : at + rng.randint(1, 16)] = bytes(rng.randrange(256) for _ in range(rng.randint(1, 16)))
    elif kind == 2:
        del data[rng.randrange(len(data)) :]
    elif kind == 3:
        struct.pack_into(
            ">I",
            data,
            4 * rng.randrange(10),
            rng.choice(
                [
                    0,
                    1,
                    0x28,
                    0xFFFFFFFF,
                    0x7FFFFFFF,
                    len(data),
                    len(data) + 1,
                    rng.randrange(1 << 32),
                    rng.randrange(len(data) + 1),
                ]
            ),
        )
    elif kind == 4:
        off_struct = min(struct.unpack_from(">I", data, 8)[0], len(data) - 4)
        at = min(len(data) - 4, off_struct + 4 * rng.randrange(max(1, (len(data) - off_struct) // 4)))
        struct.pack_into(">I", data, at, rng.choice([1, 2, 3, 4, 9, 0, 5, 0xFFFFFFFF, rng.randrange(1 << 32)]))
    elif kind == 5:
        at = rng.randrange(len(data))
        data[at:at] = bytes(rng.randrange(256) for _ in range(rng.randint(1, 64)))
    elif kind == 6:
        at = rng.randrange(len(data))
        del data[at : at + rng.randint(1, 64)]
    elif kind == 7:
        at = rng.randrange(len(data))
        data[at : at + 4] = struct.pack(">I", rng.choice([0xFFFFFFFF, 0x80000000, 0x7FFFFFFC, len(data) * 2]))
    else:
        a, b = sorted((rng.randrange(len(data)), rng.randrange(len(data))))
        data[a:b] = data[a:b][::-1]
    return bytes(data)


def fuzz(blobs, count, seed):
    rng = random.Random(seed)
    accepted = 0
    whole = [fdt.parse(b) for b in blobs]
    for i in range(count):
        data = rng.choice(blobs)
        for _ in range(rng.randint(1, 3)):
            if data:
                data = damage(rng, data)
        try:
            accepted += exercise(data, whole)
        except Exception as e:  # noqa: BLE001
            path = os.path.join(tempfile.gettempdir(), "kbuildlab-fdt-crash-%d-%d.dtb" % (seed, i))
            with open(path, "wb") as f:
                f.write(data)
            check(False, "damaged blob %d of seed %d raised %s: %s (kept as %s)" % (i, seed, type(e).__name__, e, path))
            return
    check(True, "fuzz")
    print(
        "fuzz: %d damaged blobs from seed %d, %d still well formed, none raised anything but FdtError"
        % (count, seed, accepted)
    )


def main(argv):
    count, seed = 20000, 20261116
    if "--fuzz" in argv:
        count = int(argv[argv.index("--fuzz") + 1])
    if "--seed" in argv:
        seed = int(argv[argv.index("--seed") + 1])
    for tool in ("dtc", "fdtget"):
        if shutil.which(tool) is None:
            print("tests/fdt.py: %s is not installed; nothing was checked" % tool)
            return 2
    with tempfile.TemporaryDirectory() as tmp:
        blobs = unit(tmp)
    check(exercise(deep(64)), "64 levels are read, as the kernel reads them")
    for levels in (65, 5000):
        try:
            fdt.parse(deep(levels))
            check(False, "%d levels are refused" % levels)
        except fdt.FdtError:
            check(True, "deep")
    fuzz(blobs + [deep(40)], count, seed)
    print("%d checks, %s" % (checked, "all hold" if not failed else "%d failed" % failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
