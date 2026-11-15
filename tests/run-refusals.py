#!/usr/bin/env python3
"""Hold `kbuildlab run` to what it refuses when a device tree is involved.

    run-refusals.py TREE

TREE is a registered tree that boots through U-Boot; its kernel image and its
firmware are borrowed, read-only, by a copy of the tree in a registry of this
test's own.  Every call must end with kbuildlab's message before a guest starts;
the last ones ask the copy's firmware for its device tree first, which boots it
for a moment.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile

here = os.path.dirname(os.path.abspath(__file__))
repo = os.path.dirname(here)
sys.path.insert(0, os.path.join(repo, "lib"))
import fdt  # noqa: E402

KBL = os.path.join(repo, "bin", "kbuildlab")
SHELL_ERROR = re.compile(
    r"unbound variable|syntax error|command not found|bad substitution|"
    r"ambiguous redirect|integer expression|Traceback"
)
ID = "e" * 16
failed = 0
checked = 0


def real(*args):
    r = subprocess.run([KBL, *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit("run-refusals.py: kbuildlab %s: %s" % (" ".join(args), r.stderr.strip()))
    return r.stdout.strip()


def dtc(source, out):
    subprocess.run(["dtc", "-q", "-I", "dts", "-O", "dtb", "-o", out, "-"], input=source.encode(), check=True)
    return out


def main(argv):
    global failed, checked
    if len(argv) != 1 or argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0 if argv else 2
    tree = argv[0]
    conf = real("get", tree)
    if real("get", tree, "BOOT") != "uboot":
        print("run-refusals.py: %s does not boot through U-Boot" % tree, file=sys.stderr)
        return 2
    firmware = re.search(r"^\s*image\s+(\S+)", real("firmware", tree), re.M).group(1)
    load, ramdisk = real("get", tree, "UBOOT_LOADADDR"), real("get", tree, "UBOOT_RDADDR")
    with tempfile.TemporaryDirectory() as root:
        env = dict(
            os.environ,
            XDG_CONFIG_HOME=os.path.join(root, "config"),
            XDG_DATA_HOME=os.path.join(root, "data"),
            KBL_STATE_DIR=root,
            KBL_LAB=os.path.join(root, "lab"),
            NO_COLOR="1",
        )
        env.pop("KBL_WORKSPACE", None)
        d = os.path.join(root, "config", "kbuildlab", "trees", ID)
        os.makedirs(d)
        disk = os.path.join(root, "boot.img")
        image = os.path.join(real("inspect", tree, "--format", "src"), real("get", tree, "KERNEL_IMAGE_REL"))
        with open(disk, "wb") as f:
            f.truncate(1 << (os.path.getsize(image) + (16 << 20) - 1).bit_length())
        subprocess.run(
            ["sfdisk", disk], input=b"label: dos\nstart=2048, type=0c, bootable\n", check=True, capture_output=True
        )
        subprocess.run(["mformat", "-i", disk + "@@1M", "-v", "KBLBOOT", "::"], check=True, capture_output=True)
        initrd = os.path.join(root, "initrd")
        with open(initrd, "wb") as f:
            f.write(b"\0" * 4096)
        keep = [
            line
            for line in conf.splitlines()
            if not re.match(r"\s*(UBOOT_FDTADDR|BOOTDISK|ROOT|PERSIST|INITRD|MEM|RAM_BASE)\s*=", line)
        ]
        machine, cpu = real("get", tree, "MACHINE"), real("get", tree, "CPU")

        def run(fdtaddr, *args, expect, extra="", bios: str | None = firmware):
            global failed, checked
            checked += 1
            with open(os.path.join(d, "tree.conf"), "w") as f:
                f.write(
                    extra
                    + "\n".join(keep)
                    + "\nBOOTDISK=%s\nMEM=1G\n%s" % (disk, ("UBOOT_FDTADDR=%s\n" % fdtaddr) if fdtaddr else "")
                )
            r = subprocess.run(
                [KBL, "run", ID, *(["--bios", bios] if bios else []), "--no-initrd", *args],
                env=env,
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=300,
            )
            out = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", r.stdout + r.stderr)
            why = []
            if r.returncode == 0:
                why.append("it ran")
            why += ["no %r" % word for word in ([expect] if isinstance(expect, str) else expect) if word not in out]
            m = SHELL_ERROR.search(out)
            if m:
                why.append("a shell error (%s)" % m.group(0))
            if why:
                failed += 1
                print(
                    "FAIL UBOOT_FDTADDR=%s run %s: %s\n     %s"
                    % (fdtaddr, " ".join(args), ", ".join(why), out.strip().replace("\n", "\n     ")[-700:])
                )

        good = dtc('/dts-v1/;\n/ { model = "t"; };\n', os.path.join(root, "good.dtb"))
        args = dtc('/dts-v1/;\n/ { chosen { bootargs = "x"; }; };\n', os.path.join(root, "args.dtb"))
        junk = os.path.join(root, "junk.dtb")
        with open(junk, "wb") as f:
            f.write(b"\xd0\x0d\xfe\xed" + b"\xff" * 60)
        base = int(load, 16) & ~0xFFFFFFF
        free = "%#x" % (base + (512 << 20))

        run(free, "--dtb", os.path.join(root, "missing.dtb"), expect="--dtb: no file")
        run(free, "--dtb", junk, expect="is not a well-formed device tree blob")
        run(free, "--dtb", good, "--no-dtb", expect="--dtb contradicts --no-dtb")
        run(free, "--dtb", good, "--boot", "uefi", expect="a uefi boot takes no device tree")
        run("", "--dtb", good, expect="state UBOOT_FDTADDR")
        run("47000000", "--dtb", good, expect="it is a hex address")
        run("0x10", "--dtb", good, expect="lies outside this guest's RAM")
        run("%#x" % (base + (2 << 30)), "--dtb", good, expect="lies outside this guest's RAM")
        run(load, "--dtb", good, expect="would lie in the kernel")
        run("%#x" % (int(load, 16) + 4096), "--dtb", good, expect="would lie in the kernel")
        run("%#x" % (int(load, 16) - 16), "--dtb", good, expect="would lie in the kernel")
        run(free, "--dtb", args, expect="states /chosen bootargs")
        run(free, "--dtb", good, expect="RAM_BASE", extra="MACHINE=not-%s\nIOMMU=0\n" % machine)
        run(free, "--dtb", good, expect=("qemu does not start", "not-%s" % cpu), extra="CPU=not-%s\n" % cpu)

        data = os.path.join(root, "data", "kbuildlab")
        os.makedirs(data, exist_ok=True)
        os.symlink(
            os.path.join(
                os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "kbuildlab", "firmware"
            ),
            os.path.join(data, "firmware"),
        )
        held = os.path.join(data, "trees", ID, "dt", "uboot", "firmware.dtb")
        run(free, "--dtb", good, expect="does not agree with the machine")
        if not os.path.isfile(held):
            checked += 1
            failed += 1
            print("FAIL the copy's firmware was not asked for its device tree: no %s" % held)
        else:
            t = fdt.load(held)
            ac, sc = t.root.cell("#address-cells", 2), t.root.cell("#size-cells", 1)
            at = int(load, 16)
            child = "k@%x { reg = <%s %s>; };" % (
                at,
                " ".join(["0"] * (ac - 1) + ["%#x" % at]),
                " ".join(["0"] * (sc - 1) + ["0x10000"]),
            )
            if any(n.path == "/reserved-memory" for n in t.root.children):
                body = "&{/reserved-memory} { %s };" % child
            else:
                body = "&{/} { reserved-memory { #address-cells = <%d>; #size-cells = <%d>; ranges; %s }; };" % (
                    ac,
                    sc,
                    child,
                )
            overlay = os.path.join(root, "over.dtbo")
            subprocess.run(
                ["dtc", "-q", "-@", "-I", "dts", "-O", "dtb", "-o", overlay, "-"],
                input=("/dts-v1/;\n/plugin/;\n%s\n" % body).encode(),
                check=True,
            )
            over = os.path.join(root, "over.dtb")
            subprocess.run(["fdtoverlay", "-i", held, "-o", over, overlay], check=True, capture_output=True)
            run(free, "--dtb", over, expect="covers the kernel")
            cpus = sum(1 for n in t.root.walk() if n.text("device_type") == "cpu")
            run(free, "--dtb", held, "--smp", str(cpus + 1), expect="is not the one")
            other = shutil.copy(firmware, os.path.join(root, "other-firmware.bin"))
            run(free, "--dtb", held, expect="boots another firmware", bios=other)

        built = os.path.dirname(firmware)
        named = os.path.dirname(built)
        stamp = built + ".stamp"
        if os.path.isfile(stamp) and os.path.realpath(named) == os.path.realpath(
            os.path.join(data, "firmware", os.path.basename(named))
        ):
            os.unlink(os.path.join(data, "firmware"))
            mine = os.path.join(data, "firmware", os.path.basename(named))
            os.makedirs(os.path.join(mine, os.path.basename(built)))
            os.symlink(os.path.join(named, "src"), os.path.join(mine, "src"))
            shutil.copy(firmware, os.path.join(mine, os.path.basename(built), os.path.basename(firmware)))
            with open(stamp) as f:
                lines = f.read().splitlines()
            with open(os.path.join(mine, os.path.basename(stamp)), "w") as f:
                f.write("\n".join("conf 0" if line.startswith("conf ") else line for line in lines) + "\n")
            run(free, "--no-dtb", expect="is not built from its source and settings", bios=None)
        else:
            print("run-refusals.py: %s boots a firmware of its own; a stale one was not tried" % tree)

        def with_initrd(fdtaddr, expect):
            global failed, checked
            checked += 1
            with open(os.path.join(d, "tree.conf"), "w") as f:
                f.write("\n".join(keep) + "\nBOOTDISK=%s\nMEM=1G\nUBOOT_FDTADDR=%s\n" % (disk, fdtaddr))
            r = subprocess.run(
                [KBL, "run", ID, "--bios", firmware, "--initrd", initrd, "--dtb", good],
                env=env,
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=120,
            )
            out = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", r.stdout + r.stderr)
            if r.returncode == 0 or expect not in out or SHELL_ERROR.search(out):
                failed += 1
                print("FAIL UBOOT_FDTADDR=%s with an initramfs: no %r\n     %s" % (fdtaddr, expect, out.strip()[-500:]))

        with_initrd(ramdisk, "would lie in the initramfs")
        with_initrd("%#x" % (int(ramdisk, 16) + 4000), "would lie in the initramfs")
        left = [n for n in os.listdir(root) if n.startswith("kbl-") and n != "kbl-claim.lock"]
        checked += 1
        if left:
            failed += 1
            print("FAIL the refused runs left %s in the state directory" % left)
    print("%d refusals checked, %s" % (checked, "all as they should be" if not failed else "%d failed" % failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
