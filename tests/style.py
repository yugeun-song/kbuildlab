#!/usr/bin/env python3
"""Hold a checkout to its formatters, its type checker and to English: shfmt lays
out the shell and ruff the Python, each by the settings the checkout carries
(.editorconfig, ruff.toml); ty finds nothing to report with the paths and
modules ty.toml states; and no tracked file or commit message holds a letter of
another script than Latin.

    style.py [DIR]

DIR is the checkout to hold, this one when omitted.  Nothing is written: run
`shfmt -w .` and `ruff format .` in the checkout to put right what this reports.
ty is taken from $KBL_TY, else from PATH; without one, types are not checked and
the last line says so.
"""

import os
import shutil
import subprocess
import sys

here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(here), "lib"))
import english  # noqa: E402

failed = 0
checked = 0


def check(ok, what, detail=""):
    global failed, checked
    checked += 1
    if not ok:
        failed += 1
        print("FAIL " + what + ("\n     " + detail.strip().replace("\n", "\n     ") if detail.strip() else ""))


def run(root, *cmd):
    r = subprocess.run(cmd, cwd=root, capture_output=True, text=True)
    return r.returncode, r.stdout, r.stderr


def formatted(root):
    rc, out, err = run(root, "shfmt", "-f", ".")
    shells = out.split()
    check(rc == 0 and shells, "shfmt finds the shell files of %s" % root, err)
    rc, out, err = run(root, "shfmt", "-l", ".")
    check(rc == 0 and not out.strip() and not err.strip(), "shfmt would change no shell file", out + err)
    rc, out, err = run(root, "ruff", "format", "--no-cache", "--check", "--output-format", "concise", ".")
    check(rc == 0, "ruff would change no Python file", out + err)
    return len(shells)


def typed(root):
    ty = os.environ.get("KBL_TY") or shutil.which("ty")
    if not ty:
        return False
    check(os.path.isfile(os.path.join(root, "ty.toml")), "%s carries ty.toml" % root)
    rc, out, err = run(root, ty, "check", "--output-format", "concise", ".")
    check(rc == 0, "ty reports nothing", out + err)
    return True


def in_english(root):
    rc, out, err = run(root, "git", "ls-files", "-z")
    check(rc == 0, "git lists the tracked files of %s" % root, err)
    found = []
    for name in filter(None, out.split("\0")):
        path = os.path.join(root, name)
        if not os.path.isfile(path) or os.path.islink(path):
            continue
        text = english.read(path)
        hit = english.foreign(text) if text is not None else None
        if hit:
            found.append("%s:%d: %s" % (name, hit[0], hit[1]))
    check(not found, "every tracked file is in English", "\n".join(found))
    rc, out, err = run(root, "git", "log", "--format=%h%x00%B%x01")
    check(rc == 0, "git lists the commits of %s" % root, err)
    found = []
    for record in out.split("\x01"):
        commit, _, message = record.strip().partition("\0")
        hit = english.foreign(message)
        if hit:
            found.append("%s: %s" % (commit, hit[1]))
    check(not found, "every commit message is in English", "\n".join(found))


def main(argv):
    if argv and argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0
    root = os.path.abspath(argv[0]) if argv else os.path.dirname(here)
    for tool in ("shfmt", "ruff", "git"):
        if shutil.which(tool) is None:
            print("style.py: %s is not installed" % tool, file=sys.stderr)
            return 2
    for name in (".editorconfig", "ruff.toml"):
        check(os.path.isfile(os.path.join(root, name)), "%s carries %s" % (root, name))
    shells = formatted(root)
    types = typed(root)
    in_english(root)
    print(
        "%d checks on %s (%d shell files%s), %s"
        % (
            checked,
            root,
            shells,
            "" if types else "; no ty, so types were not checked",
            "all hold" if not failed else "%d failed" % failed,
        )
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
