#!/usr/bin/env python3
"""Boot a tree with `kbuildlab run --run` on a pseudo-terminal, log in on its
console, run commands there, power it off, and print what each command said.

    guest.py TREE [RUN-OPTION...] [-- COMMAND...]

    KBL_GUEST_TIMEOUT   seconds the whole boot may take (default 900)
    KBL_GUEST_LOGIN     user and password, as USER:PASSWORD (default root:root)
    KBL_GUEST_PREFIX    words put before the kbuildlab command (taskset, nice)
    KBL_GUEST_LOG       file that receives the whole console

Status 0 when the guest reached a shell, every command ended with status 0 and
the guest powered off; the launcher's own lines are part of the output.
"""

import fcntl
import os
import pty
import re
import select
import signal
import struct
import sys
import termios
import time

here = os.path.dirname(os.path.abspath(__file__))
KBL = os.path.join(os.path.dirname(here), "bin", "kbuildlab")
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][0-9A-Za-z]|\r")
PANIC = re.compile(r"Kernel panic|Unable to mount root|Attempted to kill init")


class Guest:
    def __init__(self, tree, options, timeout):
        self.deadline = time.monotonic() + timeout
        self.text = ""
        self.seen = 0
        prefix = os.environ.get("KBL_GUEST_PREFIX", "").split()
        argv = prefix + [KBL, "run", tree, "--run"] + list(options)
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.execvp(argv[0], argv)
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 250, 0, 0))
        self.status = None

    def pump(self, wait):
        ready, _, _ = select.select([self.fd], [], [], wait)
        if not ready:
            return True
        try:
            data = os.read(self.fd, 65536)
        except OSError:
            data = b""
        if not data:
            return False
        self.text += ANSI.sub("", data.decode("utf-8", "replace"))
        return True

    def until(self, pattern, what):
        rx = re.compile(pattern)
        while True:
            m = rx.search(self.text, self.seen)
            if m:
                self.seen = m.end()
                return m
            if PANIC.search(self.text, self.seen):
                raise RuntimeError("the kernel panicked before %s" % what)
            if time.monotonic() > self.deadline:
                raise RuntimeError("timed out waiting for %s" % what)
            if not self.pump(1.0):
                raise RuntimeError("the guest ended before %s" % what)

    def send(self, line):
        os.write(self.fd, line.encode() + b"\n")

    def login(self):
        user, _, password = os.environ.get("KBL_GUEST_LOGIN", "root:root").partition(":")
        self.until(r"login: ", "a login prompt")
        self.send(user)
        m = self.until(r"Password: |[#$] $", "a password prompt or a shell")
        if m.group(0).startswith("Password"):
            self.send(password)
            self.until(r"[#$] $", "a shell")

    def run(self, n, command):
        self.send("echo KBL''BEGIN%d; %s; echo KBL''END%d $?" % (n, command, n))
        self.until(r"KBLBEGIN%d\n" % n, "command %d to start" % n)
        start = self.seen
        m = self.until(r"KBLEND%d (\d+)" % n, "command %d to end" % n)
        return int(m.group(1)), self.text[start : m.start()]

    def off(self):
        self.send("poweroff")
        while self.pump(1.0):
            if time.monotonic() > self.deadline:
                raise RuntimeError("timed out waiting for the guest to power off")
        _, status = os.waitpid(self.pid, 0)
        self.status = os.waitstatus_to_exitcode(status)
        return self.status

    def kill(self):
        if self.status is not None:
            return
        try:
            os.kill(self.pid, signal.SIGTERM)
            for _ in range(100):
                if os.waitpid(self.pid, os.WNOHANG)[0]:
                    break
                self.pump(0.1)
            else:
                os.kill(self.pid, signal.SIGKILL)
                os.waitpid(self.pid, 0)
        except (ProcessLookupError, ChildProcessError):
            pass


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0
    commands = []
    if "--" in argv:
        at = argv.index("--")
        argv, commands = argv[:at], argv[at + 1 :]
    guest = Guest(argv[0], argv[1:], float(os.environ.get("KBL_GUEST_TIMEOUT", "900")))
    rc, error = 0, None
    try:
        guest.login()
        for n, command in enumerate(commands):
            status, out = guest.run(n, command)
            print("== %s (status %d)\n%s" % (command, status, out.rstrip("\n")))
            rc = rc or (1 if status else 0)
        if guest.off() != 0:
            rc = rc or 1
            error = "kbuildlab run ended with status %d" % guest.status
    except RuntimeError as e:
        rc, error = 1, str(e)
    finally:
        guest.kill()
        log = os.environ.get("KBL_GUEST_LOG")
        if log:
            with open(log, "w") as f:
                f.write(guest.text)
    if error:
        print("guest.py: %s\n%s" % (error, "\n".join(guest.text.splitlines()[-25:])), file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
