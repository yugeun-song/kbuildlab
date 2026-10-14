#!/usr/bin/env python3
import fcntl
import os
import select
import signal
import socket
import struct
import sys
import termios
import time


def size(fd):
    try:
        rows, cols = struct.unpack("HHHH", fcntl.ioctl(fd, termios.TIOCGWINSZ, bytes(8)))[:2]
    except OSError:
        return None
    return (rows, cols) if rows and cols else None


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True


def connect(path, pid):
    while alive(pid):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(path)
            return sock
        except OSError:
            sock.close()
            time.sleep(0.05)
    return None


def serve(sock, tty, pid):
    wake_r, wake_w = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
    signal.set_wakeup_fd(wake_w)
    signal.signal(signal.SIGWINCH, lambda *_: None)
    sent, guest, asked = None, False, False
    while True:
        ready, _, _ = select.select([sock, wake_r], [], [], 1.0)
        if wake_r in ready:
            os.read(wake_r, 256)
        if sock in ready:
            data = sock.recv(256)
            if not data:
                return
            if b"?" in data:
                guest = asked = True
        if not alive(pid):
            return
        now = size(tty)
        if guest and now and (asked or now != sent):
            sock.sendall(b"%d %d\n" % now)
            sent, asked = now, False


def main():
    if len(sys.argv) != 4:
        sys.exit("usage: winsize.py SOCKET QEMU_PID TTY_FD")
    path, pid, tty = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
    sock = connect(path, pid)
    if sock is None:
        return
    try:
        serve(sock, tty, pid)
    except OSError:
        pass
    finally:
        sock.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
