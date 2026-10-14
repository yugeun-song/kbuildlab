#!/usr/bin/env python3
"""kbuildlab's two tree.conf readers -- kbl_tree_get (lib/common.sh), the reference,
and clangd.py's tree_conf_get -- against tree-conf-vectors.json.  A "value" of
null means the key is unset: kbl_tree_get prints nothing, tree_conf_get returns None.
Run it as python3 -B tests/tree-conf.py."""

import json
import os
import subprocess
import sys
import tempfile

here = os.path.dirname(os.path.abspath(__file__))
repo = os.path.dirname(here)
sys.path.insert(0, os.path.join(repo, "lib", "index"))
import clangd  # noqa: E402

failed = 0
with open(os.path.join(here, "tree-conf-vectors.json")) as f:
    cases = json.load(f)
with tempfile.TemporaryDirectory() as d:
    for case in cases:
        with open(os.path.join(d, "tree.conf"), "w", newline="") as f:
            f.write(case["conf"])
        sh = subprocess.run(["bash", "-c", 'source "$1/lib/common.sh"; kbl_tree_get "$2" "$3"',
                             "-", repo, d, case["key"]], capture_output=True, text=True).stdout
        sh = sh[:-1] if sh.endswith("\n") else sh
        py = clangd.tree_conf_get(d, case["key"])
        want = case["value"]
        if sh != (want or "") or py != want:
            failed += 1
            print(f"FAIL {case['name']}: want {want!r}, kbl_tree_get {sh!r}, tree_conf_get {py!r}")
print(f"{len(cases) - failed}/{len(cases)} tree.conf cases agree")
sys.exit(1 if failed else 0)
