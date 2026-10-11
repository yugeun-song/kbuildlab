#!/usr/bin/env python3
"""One QMP command to a running guest's monitor on 127.0.0.1.

    qmp.py --port N [--timeout S] query-status   prints the runstate
    qmp.py --port N [--timeout S] cont           prints OK

Exit status 0 on a reply, 1 with the reason on stderr otherwise.
"""

import argparse
import json
import socket
import sys


def command(port: int, name: str, timeout: float) -> dict:
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
        stream = s.makefile("rb")

        def reply() -> dict:
            # Events may arrive before the reply, or in the same segment.
            for line in stream:
                msg = json.loads(line)
                if "return" in msg or "error" in msg:
                    return msg
            raise ConnectionError("the monitor closed the connection")

        json.loads(stream.readline())
        s.sendall(b'{"execute":"qmp_capabilities"}\n')
        reply()
        s.sendall(json.dumps({"execute": name}).encode() + b"\n")
        return reply()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--timeout", type=float, default=5.0)
    ap.add_argument("name", choices=("query-status", "cont"))
    args = ap.parse_args()
    try:
        msg = command(args.port, args.name, args.timeout)
    except (OSError, ValueError) as e:
        print(e, file=sys.stderr)
        return 1
    if "error" in msg:
        print(msg["error"].get("desc", msg["error"]), file=sys.stderr)
        return 1
    print(msg["return"]["status"] if args.name == "query-status" else "OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
