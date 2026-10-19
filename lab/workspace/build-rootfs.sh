#!/usr/bin/env bash
# Build the buildroot root file system (observability rootfs) for one arch, in the
# Ubuntu container (avoids Arch/buildroot conflicts, per project note). No kernel
# is built here -- the 7 trees supply their own; this is userspace only.
# Output: rootfs/<arch>/images/rootfs.cpio.gz, usable as:
#   kbuildlab run <tree> --run -- -initrd rootfs/<arch>/images/rootfs.cpio.gz
# and rootfs/<arch>/images/rootfs.ext4 (sparse; a symlink to rootfs.ext2), the root
# partition of the disk boots.
# Uses the same container runtime kbuildlab picks (podman first, then docker), so a
# host that keeps dockerd off needs no group and no daemon. Foreground by design.
set -uo pipefail
# The workspace is where this script lives, not a path written down here: a
# hardcoded root is wrong the moment the checkout is cloned, moved or shared, and
# it fails by building the wrong tree rather than by saying anything.
#
# Self-location WINS over $KBL_WORKSPACE, which is the same rule sync-all.sh
# applies (`export KBL_WORKSPACE="$HERE"`).  The alternative -- letting the
# environment override -- would have this script and sync-all.sh disagree about
# which workspace is authoritative when both are set, and the rootfs it produced
# would land in one tree while the kernels were built in another.  The scripts
# in a workspace belong to THAT workspace.
ROOT="$(cd -P "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
[ -d "$ROOT/rootfs" ] || { echo "build-rootfs: $ROOT does not look like the research workspace (no rootfs/)" >&2; exit 1; }
export KBL_WORKSPACE="$ROOT"
BR="$ROOT/.buildroot/buildroot"
IMG="${KBL_IMAGE:-kbuildlab:latest}"
# Same probe order as lib/common.sh, and $KBL_CONTAINER wins, so this script and
# kbuildlab cannot end up using different runtimes on the same machine.
CRT=""
for c in ${KBL_CONTAINER:-} podman docker nerdctl; do
  command -v "$c" >/dev/null 2>&1 && { CRT="$c"; break; }
done
[ -n "$CRT" ] || { echo "no container runtime (podman, docker or nerdctl)"; exit 1; }
# Rootless podman already maps the invoking user onto the container's root; --user
# there would put the build on a subuid and the output would come back unwritable.
IDOPT=""
if [ "$(basename "$CRT")" != podman ] || [ "$(id -u)" -eq 0 ]; then
  IDOPT="--user $(id -u):$(id -g) -e HOME=/tmp"
fi
arch="${1:?usage: build-rootfs.sh <x86_64|arm64|riscv64>}"
def="$ROOT/rootfs/defconfig-$arch"
[ -f "$def" ] || { echo "no defconfig: $def"; exit 1; }
out="$ROOT/rootfs/$arch"
mkdir -p "$out" "$ROOT/.buildroot/dl"

# shellcheck disable=SC2086  # IDOPT is a deliberate word-split option list
# The container's exit status is this script's exit status.  It used to end with
# an `ls` of the images directory, which always succeeds -- so a build that
# refused to start (buildroot's host-dependency check, a missing toolchain) was
# reported as success and the caller went on to package a rootfs from months ago.
rc=0
"$CRT" run --rm $IDOPT \
  -v "$ROOT:$ROOT" -w "$BR" "$IMG" sh -c "
    set -e
    export BR2_DL_DIR='$ROOT/.buildroot/dl' FORCE_UNSAFE_CONFIGURE=1
    make O='$out' BR2_DEFCONFIG='$def' defconfig
    # The defconfig names post-build.sh by absolute path, which is only right for
    # the workspace it was written in.  Point it at THIS one, in the generated
    # .config, so a moved or cloned workspace fails at once (or not at all)
    # instead of after building an entire toolchain.
    sed -i 's|^BR2_ROOTFS_POST_BUILD_SCRIPT=.*|BR2_ROOTFS_POST_BUILD_SCRIPT=\"$ROOT/rootfs/post-build.sh\"|' '$out/.config'
    grep -q '^BR2_ROOTFS_POST_BUILD_SCRIPT=\"$ROOT/rootfs/post-build.sh\"' '$out/.config' \
        || { echo 'build-rootfs: could not point BR2_ROOTFS_POST_BUILD_SCRIPT at this workspace' >&2; exit 1; }
    make O='$out' -j${KBL_JOBS:-5}
  " || rc=$?
echo '--- images ---'
ls -la "$out/images/" 2>/dev/null
if [ "$rc" -ne 0 ]; then
  echo "build-rootfs: the container build failed (exit $rc); anything under $out/images is from an EARLIER run" >&2
  exit "$rc"
fi
for img in rootfs.cpio.gz rootfs.ext4; do
  [ -e "$out/images/$img" ] || { echo "build-rootfs: the build succeeded but left no $out/images/$img" >&2; exit 1; }
done
