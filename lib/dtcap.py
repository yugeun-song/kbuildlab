#!/usr/bin/env python3
"""Copy a device tree out of a running guest's memory, over QMP.

    dtcap.py --qmp SOCKET --addr ADDR --limit BYTES --out FILE
             [--bootargs TEXT] [--pid PID] [--timeout SECONDS]

Waits until a well-formed tree stands at ADDR and reads the same twice; with
--bootargs, until its /chosen bootargs is TEXT.  Ends without one when PID is
gone or the timeout passes.
"""
import argparse
import json
import os
import socket
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fdt  # noqa: E402

INTERVAL = 0.3
REPLY_TIMEOUT = 5.0


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True


def read_guest(path, addr, size, scratch):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(REPLY_TIMEOUT)
        s.connect(path)
        stream = s.makefile("rb")

        def reply():
            for line in stream:
                msg = json.loads(line)
                if "return" in msg or "error" in msg:
                    return msg
            raise ConnectionError("the monitor closed the connection")

        json.loads(stream.readline())
        s.sendall(b'{"execute":"qmp_capabilities"}\n')
        reply()
        s.sendall(json.dumps({"execute": "pmemsave", "arguments": {
            "val": addr, "size": size, "filename": scratch}}).encode() + b"\n")
        if "error" in reply():
            return None
    with open(scratch, "rb") as f:
        return f.read()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--qmp", required=True)
    ap.add_argument("--addr", required=True, type=lambda v: int(v, 0))
    ap.add_argument("--limit", required=True, type=lambda v: int(v, 0))
    ap.add_argument("--out", required=True)
    ap.add_argument("--bootargs")
    ap.add_argument("--pid", type=int)
    ap.add_argument("--timeout", type=float)
    a = ap.parse_args()
    scratch = a.out + ".part"
    start, last = time.monotonic(), None
    try:
        while True:
            if a.pid is not None and not alive(a.pid):
                return 1
            if a.timeout is not None and time.monotonic() - start > a.timeout:
                return 1
            time.sleep(INTERVAL)
            try:
                head = read_guest(a.qmp, a.addr, fdt.HEADER, scratch)
                if not head or len(head) < fdt.HEADER:
                    continue
                magic, total = struct.unpack(">II", head[:8])
                if magic != fdt.MAGIC or not fdt.HEADER < total <= a.limit:
                    last = None
                    continue
                blob = read_guest(a.qmp, a.addr, total, scratch)
                tree = fdt.parse(blob)
            except (OSError, ValueError, TypeError):
                last = None
                continue
            if a.bootargs is not None:
                chosen = tree.node("/chosen")
                if chosen is None or chosen.text("bootargs") != a.bootargs:
                    last = None
                    continue
            if blob != last:
                last = blob
                continue
            with open(scratch, "wb") as f:
                f.write(blob)
            os.replace(scratch, a.out)
            return 0
    finally:
        try:
            os.unlink(scratch)
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
