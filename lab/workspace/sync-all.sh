#!/usr/bin/env bash
# Update every tree: `kbuildlab update` per tree = sync (fetch+ff, full history
# kept; a pinned tag is a no-op) -> config (olddefconfig, keeps .config) -> build
# (incremental) -> tags (ctags+cscope+gtags).
#
# Rootless podman is the default runtime and needs no group and no daemon.
set -uo pipefail
HERE="$(cd -P "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
export KBL_WORKSPACE="$HERE"
export KBL_JOBS="${KBL_JOBS:-5}"
# Which kbuildlab.  $KBL_REPO names a checkout explicitly; otherwise the one on
# $PATH, which is the installed name and resolves through its symlink.  The
# absolute path that used to be the default was correct on exactly one machine
# and, being a default rather than an error, would have run a DIFFERENT checkout
# than the one the operator meant on any other.
if [[ -n "${KBL_REPO:-}" ]]; then
    KBL="${KBL_REPO}/bin/kbuildlab"
else
    KBL="$(command -v kbuildlab || command -v kbl || true)"
fi
[[ -n "$KBL" && -x "$KBL" ]] || {
    echo "sync-all: no kbuildlab -- put it on \$PATH, or set \$KBL_REPO to its checkout" >&2
    exit 1
}
say_kbl="$(readlink -f "$KBL")"
echo "sync-all: using $say_kbl"

# Optional: limit to trees named on the command line; default is all seven.
if [[ $# -gt 0 ]]; then trees=("$@"); else
  trees=(v6.12-x86_64 v6.12-arm64 v6.12-riscv64
         upstream-x86_64 upstream-arm64 upstream-riscv64
         v4.6-arm64)
fi

# No per-tree special-casing here any more.  A tree states its own toolchain in its
# own tree.conf -- GCC=, and IMAGE= if it needs another image -- which is where the
# rest of its build is described.  All seven state GCC=16; v4.6 additionally states
# HOSTCC_FLAGS and KCFLAGS, and none needs a different image.
rc=0
for t in "${trees[@]}"; do
  echo "######## update $t ########"
  "$KBL" update "$t" || { echo "!! $t update FAILED"; rc=1; }
done
echo "sync-all done (rc=$rc)"
exit $rc
