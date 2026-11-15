#!/usr/bin/env python3
"""Hold `kbuildlab run` to what it refuses when a device tree is involved.

    run-refusals.py TREE

TREE is a registered tree that boots through U-Boot; its kernel image and its
firmware are borrowed, read-only, by a copy of the tree in a registry of this
test's own.  Every call must end with kbuildlab's message before a guest starts.
"""
import os
import re
import subprocess
import sys
import tempfile

here = os.path.dirname(os.path.abspath(__file__))
repo = os.path.dirname(here)
KBL = os.path.join(repo, "bin", "kbuildlab")
SHELL_ERROR = re.compile(r"unbound variable|syntax error|command not found|bad substitution|"
                         r"ambiguous redirect|integer expression|Traceback")
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
        env = dict(os.environ, XDG_CONFIG_HOME=os.path.join(root, "config"), XDG_DATA_HOME=os.path.join(root, "data"),
                   KBL_STATE_DIR=root, KBL_LAB=os.path.join(root, "lab"), NO_COLOR="1")
        env.pop("KBL_WORKSPACE", None)
        d = os.path.join(root, "config", "kbuildlab", "trees", ID)
        os.makedirs(d)
        disk = os.path.join(root, "boot.img")
        image = os.path.join(real("inspect", tree, "--format", "src"), real("get", tree, "KERNEL_IMAGE_REL"))
        with open(disk, "wb") as f:
            f.truncate(os.path.getsize(image) + (16 << 20))
        subprocess.run(["sfdisk", disk], input=b"label: dos\nstart=2048, type=0c, bootable\n", check=True,
                       capture_output=True)
        subprocess.run(["mformat", "-i", disk + "@@1M", "-v", "KBLBOOT", "::"], check=True, capture_output=True)
        initrd = os.path.join(root, "initrd")
        with open(initrd, "wb") as f:
            f.write(b"\0" * 4096)
        keep = [line for line in conf.splitlines()
                if not re.match(r"\s*(UBOOT_FDTADDR|BOOTDISK|ROOT|PERSIST|INITRD|MEM|RAM_BASE)\s*=", line)]
        machine, cpu = real("get", tree, "MACHINE"), real("get", tree, "CPU")

        def run(fdtaddr, *args, expect, extra=""):
            global failed, checked
            checked += 1
            with open(os.path.join(d, "tree.conf"), "w") as f:
                f.write(extra + "\n".join(keep) + "\nBOOTDISK=%s\nMEM=1G\n%s" % (
                    disk, ("UBOOT_FDTADDR=%s\n" % fdtaddr) if fdtaddr else ""))
            r = subprocess.run([KBL, "run", ID, "--bios", firmware, "--no-initrd", *args], env=env,
                               capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120)
            out = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", r.stdout + r.stderr)
            why = []
            if r.returncode == 0:
                why.append("it ran")
            if expect not in out:
                why.append("no %r" % expect)
            m = SHELL_ERROR.search(out)
            if m:
                why.append("a shell error (%s)" % m.group(0))
            if why:
                failed += 1
                print("FAIL UBOOT_FDTADDR=%s run %s: %s\n     %s" % (fdtaddr, " ".join(args), ", ".join(why),
                                                                    out.strip().replace("\n", "\n     ")[-700:]))

        good = dtc('/dts-v1/;\n/ { model = "t"; };\n', os.path.join(root, "good.dtb"))
        args = dtc('/dts-v1/;\n/ { chosen { bootargs = "x"; }; };\n', os.path.join(root, "args.dtb"))
        junk = os.path.join(root, "junk.dtb")
        with open(junk, "wb") as f:
            f.write(b"\xd0\x0d\xfe\xed" + b"\xff" * 60)
        base = int(load, 16) & ~0xfffffff
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
        run(free, "--dtb", good, expect="qemu does not start", extra="CPU=not-%s\n" % cpu)

        def with_initrd(fdtaddr, expect):
            global failed, checked
            checked += 1
            with open(os.path.join(d, "tree.conf"), "w") as f:
                f.write("\n".join(keep) + "\nBOOTDISK=%s\nMEM=1G\nUBOOT_FDTADDR=%s\n" % (disk, fdtaddr))
            r = subprocess.run([KBL, "run", ID, "--bios", firmware, "--initrd", initrd, "--dtb", good], env=env,
                               capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120)
            out = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", r.stdout + r.stderr)
            if r.returncode == 0 or expect not in out or SHELL_ERROR.search(out):
                failed += 1
                print("FAIL UBOOT_FDTADDR=%s with an initramfs: no %r\n     %s" % (fdtaddr, expect, out.strip()[-500:]))

        with_initrd(ramdisk, "would lie in the initramfs")
        with_initrd("%#x" % (int(ramdisk, 16) + 4000), "would lie in the initramfs")
        left = [n for n in os.listdir(root) if n.startswith("kbl-")]
        checked += 1
        if left:
            failed += 1
            print("FAIL the refused runs left %s in the state directory" % left)
    print("%d refusals checked, %s" % (checked, "all as they should be" if not failed else "%d failed" % failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
