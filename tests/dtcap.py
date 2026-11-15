#!/usr/bin/env python3
"""Hold lib/dtcap.py against a monitor that misbehaves: a stand-in QMP server
serves `pmemsave` from a memory image that this test changes while dtcap polls.
No guest is started."""
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time

here = os.path.dirname(os.path.abspath(__file__))
repo = os.path.dirname(here)
sys.path.insert(0, os.path.join(repo, "lib"))
import fdt  # noqa: E402

DTCAP = os.path.join(repo, "lib", "dtcap.py")
ADDR = 0x1000
failed = 0
checked = 0


def check(ok, what):
    global failed, checked
    checked += 1
    if not ok:
        failed += 1
        print("FAIL " + what)


class Monitor(threading.Thread):
    def __init__(self, path, memory, step=None, mode=""):
        super().__init__(daemon=True)
        self.memory, self.step, self.mode, self.reads = bytearray(memory), step, mode, 0
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(path)
        self.sock.listen(8)
        self.stop = False

    def run(self):
        while not self.stop:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                try:
                    self.serve(conn)
                except OSError:
                    pass

    def serve(self, conn):
        if self.mode == "hangup":
            return
        conn.sendall(b'{"QMP": {"version": {}, "capabilities": []}}\r\n')
        if self.mode == "silent":
            time.sleep(0.5)
            return
        stream = conn.makefile("rb")
        for line in stream:
            msg = json.loads(line)
            if self.mode == "events":
                conn.sendall(b'{"event": "RESUME", "timestamp": {"seconds": 1, "microseconds": 2}}\r\n')
            if msg["execute"] == "pmemsave":
                self.reads += 1
                if self.step:
                    self.step(self.reads, self.memory)
                if self.mode == "error":
                    conn.sendall(b'{"error": {"class": "GenericError", "desc": "Invalid addr"}}\r\n')
                    continue
                a = msg["arguments"]
                with open(a["filename"], "wb") as f:
                    f.write(bytes(self.memory[a["val"]:a["val"] + a["size"]]))
            conn.sendall(b'{"return": {}}\r\n')

    def close(self):
        self.stop = True
        self.sock.close()


def blob(tmp, bootargs=None, name="t.dtb"):
    chosen = ' chosen { bootargs = "%s"; };' % bootargs if bootargs is not None else ""
    path = os.path.join(tmp, name)
    subprocess.run(["dtc", "-q", "-I", "dts", "-O", "dtb", "-o", path, "-"], check=True,
                   input=('/dts-v1/;\n/ { model = "t";%s memory@0 { device_type = "memory"; reg = <0 0x100>; }; };\n' % chosen).encode())
    with open(path, "rb") as f:
        return f.read()


def capture(tmp, memory, *options, step=None, mode="", timeout="6"):
    sock = os.path.join(tmp, "q.sock")
    out = os.path.join(tmp, "out.dtb")
    for p in (sock, out):
        if os.path.exists(p):
            os.unlink(p)
    mon = Monitor(sock, memory, step, mode)
    mon.start()
    try:
        r = subprocess.run([sys.executable, "-I", DTCAP, "--qmp", sock, "--addr", hex(ADDR), "--limit", "65536",
                            "--out", out, "--timeout", timeout, *options], capture_output=True, text=True, timeout=60)
    finally:
        mon.close()
    data = None
    if os.path.exists(out):
        with open(out, "rb") as f:
            data = f.read()
    left = [n for n in os.listdir(tmp) if n.endswith(".part")]
    return r.returncode, data, mon.reads, left, r.stderr


def image(data, size=1 << 16):
    m = bytearray(size)
    m[ADDR:ADDR + len(data)] = data
    return m


def main():
    with tempfile.TemporaryDirectory() as tmp:
        plain, args, other = blob(tmp), blob(tmp, "console=x root=y"), blob(tmp, "console=other")

        rc, data, reads, left, err = capture(tmp, image(plain))
        check(rc == 0 and data == plain and not left, "a tree that stands still is copied whole, and nothing is left beside it: %s" % err)
        check(reads >= 4, "it is read twice before it is believed (%d reads)" % reads)

        rc, data, _, left, _ = capture(tmp, bytearray(1 << 16), timeout="1.5")
        check(rc == 1 and data is None and not left, "memory without a tree ends in a timeout and no file")

        def appear(n, memory):
            if n == 5:
                memory[ADDR:ADDR + len(plain)] = plain
        rc, data, _, _, _ = capture(tmp, bytearray(1 << 16), step=appear)
        check(rc == 0 and data == plain, "a tree that appears later is waited for")

        def growing(n, memory):
            if n <= 12:
                part = plain[:max(40, len(plain) * n // 12)]
                memory[ADDR:ADDR + len(plain)] = part + b"\0" * (len(plain) - len(part))
        rc, data, _, _, _ = capture(tmp, bytearray(1 << 16), step=growing)
        check(rc == 0 and data == plain, "a tree still being written is not taken half-written")

        def rewritten(n, memory):
            if n == 7:
                memory[ADDR:ADDR + len(args)] = args
        rc, data, _, _, _ = capture(tmp, image(other), "--bootargs", "console=x root=y", step=rewritten)
        check(rc == 0 and data == args, "with --bootargs, the tree is taken only once its /chosen says that")
        rc, data, _, _, _ = capture(tmp, image(other), "--bootargs", "console=x root=y", timeout="1.5")
        check(rc == 1 and data is None, "a tree with other bootargs is never taken")
        rc, data, _, _, _ = capture(tmp, image(plain), "--bootargs", "console=x root=y", timeout="1.5")
        check(rc == 1 and data is None, "a tree without /chosen is never taken when bootargs are asked for")

        def flicker(n, memory):
            memory[ADDR + 8 + (n % 2)] ^= 0xff if n < 9 else 0
        rc, data, _, _, _ = capture(tmp, image(plain), step=flicker)
        check(rc == 0 and data == plain, "a tree that keeps changing is taken only when two reads agree")

        huge = bytearray(plain)
        struct.pack_into(">I", huge, 4, 1 << 30)
        rc, data, reads, _, _ = capture(tmp, image(bytes(huge)), timeout="1.5")
        check(rc == 1 and data is None, "a header that states more than the limit is not followed")
        bad = bytearray(plain)
        struct.pack_into(">I", bad, 8, 0x7fffffff)
        rc, data, _, _, _ = capture(tmp, image(bytes(bad)), timeout="1.5")
        check(rc == 1 and data is None, "a blob that does not parse is not taken")

        for mode, what in (("error", "a monitor that answers with an error"), ("hangup", "a monitor that hangs up"),
                           ("silent", "a monitor that greets and says no more")):
            rc, data, _, left, err = capture(tmp, image(plain), mode=mode, timeout="2.5")
            check(rc == 1 and data is None and not left and "Traceback" not in err, "%s ends in a timeout: %s" % (what, err[-200:]))
        rc, data, _, _, err = capture(tmp, image(plain), mode="events")
        check(rc == 0 and data == plain, "events between the replies are passed over: %s" % err[-200:])

        gone = subprocess.Popen(["true"])
        gone.wait()
        t0 = time.monotonic()
        rc, data, _, _, _ = capture(tmp, bytearray(1 << 16), "--pid", str(gone.pid), timeout="30")
        check(rc == 1 and data is None and time.monotonic() - t0 < 5, "a guest that is gone ends the wait at once")

        check(fdt.parse(plain) is not None, "the test's own blob is well formed")
    print("%d checks, %s" % (checked, "all hold" if not failed else "%d failed" % failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
