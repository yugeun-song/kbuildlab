#!/usr/bin/env python3
"""Name the files that hold a letter of another script than Latin, which is what
this repository takes for text that is not English.

    english.py [-q] FILE...

Prints FILE:LINE and the letters found, for the first such line of each file;
nothing with -q.  Exits 1 when a file holds one and 0 when none does.  A file
that is not text passes.
"""

import sys
import unicodedata


def foreign(text):
    for number, line in enumerate(text.splitlines(), 1):
        letters = sorted({c for c in line if c.isalpha() and "LATIN" not in unicodedata.name(c, "")})
        if letters:
            return number, "".join(letters)
    return None


def read(path):
    with open(path, "rb") as f:
        data = f.read()
    if b"\0" in data:
        return None
    return data.decode("utf-8", errors="replace")


def main(argv):
    quiet = argv[:1] == ["-q"]
    paths = argv[1:] if quiet else argv
    if not paths or paths[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0 if paths else 2
    found = 0
    for path in paths:
        try:
            text = read(path)
        except OSError as e:
            print("english.py: %s" % e, file=sys.stderr)
            return 2
        hit = foreign(text) if text is not None else None
        if hit:
            found += 1
            if not quiet:
                print("%s:%d: %s" % (path, hit[0], hit[1]))
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
