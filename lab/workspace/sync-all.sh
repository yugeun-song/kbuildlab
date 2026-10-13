#!/usr/bin/env bash
# Update every registered tree, or the ones named: `kbuildlab update` = check ->
# sync (fetch+ff, full history kept; a pinned tag is a no-op) -> config
# (olddefconfig, keeps .config) -> build (incremental) -> tags (ctags, cscope,
# gtags, clangd), then a summary.
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

# A tree states its own toolchain in its tree.conf -- GCC=, and IMAGE= if it needs
# another image -- so nothing here is per tree.
if [[ $# -gt 0 ]]; then "$KBL" update "$@"; else "$KBL" update --all; fi
