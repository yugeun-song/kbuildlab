#!/bin/sh
# Stage two things buildroot leaves out of the target:
#  - libsframe: libbfd needs it at runtime; without it bpftool fails to load.
#  - perf: built separately from each kernel's tools/perf (buildroot's perf needs
#    a full kernel build); dropped in as rootfs/perf-<arch> if present.
set -e
TARGET="$1"
for f in "$STAGING_DIR"/usr/lib/libsframe.so.1*; do
    [ -e "$f" ] && cp -a "$f" "$TARGET/usr/lib/"
done
ROOTFS_BASE="$(dirname "$(dirname "$TARGET")")"
ARCH="$(basename "$(dirname "$TARGET")")"
if [ -f "$ROOTFS_BASE/perf-$ARCH" ]; then
    cp "$ROOTFS_BASE/perf-$ARCH" "$TARGET/usr/bin/perf"
    chmod +x "$TARGET/usr/bin/perf"
fi

# Bring eth0 up via DHCP at boot. BR2_SYSTEM_DHCP only writes this stanza on a
# clean build; do it here so incremental rebuilds keep the network working
# (QEMU user-mode networking hands out 10.0.2.15, gw/DNS 10.0.2.2/10.0.2.3).
IFACES="$TARGET/etc/network/interfaces"
if [ -f "$IFACES" ] && ! grep -q "iface eth0" "$IFACES"; then
    cat >> "$IFACES" <<'EOF'

auto eth0
iface eth0 inet dhcp
EOF
fi

# Let the host reach the guest over SSH (for editing files in the guest from
# neovim, e.g. nvim scp://root@localhost:2222//path). This is a local dev VM on
# user-mode networking, so root password login is fine; the password is "root".
SSHD="$TARGET/etc/ssh/sshd_config"
if [ -f "$SSHD" ]; then
    sed -i '/^\s*#\?\s*PermitRootLogin/d;   /^\s*#\?\s*PasswordAuthentication/d' "$SSHD"
    cat >> "$SSHD" <<'EOF'
PermitRootLogin yes
PasswordAuthentication yes
EOF
fi
# If the host user has an SSH public key, trust it too, so key auth works with no
# password prompt (staged as rootfs/authorized_keys by build-rootfs.sh).
if [ -f "$ROOTFS_BASE/authorized_keys" ]; then
    mkdir -p "$TARGET/root/.ssh"
    cp "$ROOTFS_BASE/authorized_keys" "$TARGET/root/.ssh/authorized_keys"
    chmod 700 "$TARGET/root/.ssh"; chmod 600 "$TARGET/root/.ssh/authorized_keys"
fi

# Auto-mount the per-tree PERSISTENT disk at /persist. `kbuildlab run --persist`
# attaches a virtio-blk disk carrying the QEMU serial "kblpersist"; this init
# script finds it by that serial (robust against device-name order, and against
# the boot disk on the u-boot/UEFI chains), formats it ext4 on first use, and
# mounts it. It is never fatal: if no such disk is attached, or format/mount
# fails, /persist is simply left unmounted and boot continues. Survives reboot
# because the backing image lives on the host, per tree.
PERSIST="$TARGET/etc/init.d/S45persist"
cat > "$PERSIST" <<'PEOF'
#!/bin/sh
# Mount the persistent per-tree disk (host virtio-blk serial "kblpersist") at
# /persist. Non-fatal by design -- any miss leaves /persist unmounted.
SERIAL=kblpersist
LABEL=kblpersist
MP=/persist

find_dev() {
    # 1) by QEMU-assigned serial: deterministic, boot-disk-proof
    for s in /sys/block/vd*/serial /sys/block/*/serial; do
        [ -r "$s" ] || continue
        if [ "$(cat "$s" 2>/dev/null)" = "$SERIAL" ]; then
            echo "/dev/$(basename "$(dirname "$s")")"; return 0
        fi
    done
    # 2) by ext4 label: already formatted, serial not exposed
    d=$(blkid -L "$LABEL" 2>/dev/null)
    [ -n "$d" ] && { echo "$d"; return 0; }
    return 1
}

start() {
    dev=$(find_dev) || { return 0; }
    mkdir -p "$MP"
    # Mount first. A populated persist disk (the normal case: the host formats it
    # at creation) mounts straight away, so its data is NEVER at risk of a reformat.
    # Only a disk with no valid filesystem fails to mount -- format that once and
    # retry. This avoids relying on busybox blkid's has-a-filesystem exit status,
    # which is not dependable.
    if ! mount "$dev" "$MP" 2>/dev/null; then
        mke2fs -t ext4 -L "$LABEL" -q -F "$dev" >/dev/null 2>&1 \
            && mount "$dev" "$MP" 2>/dev/null \
            || { echo "persist: could not mount $dev; /persist unavailable"; return 0; }
    fi
    echo "persist: $dev mounted at $MP"
}

stop() {
    mountpoint -q "$MP" 2>/dev/null && umount "$MP" 2>/dev/null
    return 0
}

case "$1" in
    start|"") start ;;
    stop)     stop ;;
    restart)  stop; start ;;
    *)        echo "usage: $0 {start|stop|restart}"; exit 1 ;;
esac
PEOF
chmod 0755 "$PERSIST"

# Bring the CRNG up at boot on kernels that predate the ChaCha20 rewrite (< 4.8).
#
# Measured on v4.6.7/arm64 under QEMU: virtio-rng's khwrngd fills the INPUT pool
# to ~1300 bits by t=2s, but getrandom() waits on the separate NONBLOCKING pool,
# and that pool is only refilled by xfer_secondary_pool() -- which runs on a READ
# of /dev/urandom and is rate-limited to once per
# kernel.random.urandom_min_reseed_secs (default 60).  The one early read is
# seedrng's, before the input pool has anything to give; after that nothing reads
# urandom until sshd's ssh-keygen, which does not read at all -- it blocks in
# getrandom().  So the guest sat with an entropy surplus and an uninitialised
# CRNG for ~96 seconds, and rcS never reached the getty.
#
# Proof, from a live guest (drivers/char/random.c:986-993 is the gate):
#   t=13.8s  read urandom with the gate at 60  -> CRNG still not initialised
#   t=18.8s  echo 0 > urandom_min_reseed_secs  -> "nonblocking pool is
#                                                  initialized" in the same ms
#   t=26.7s  ssh-keygen -t ed25519             -> completed, no blocking
#
# This is not a workaround for a symptom: the pool has the entropy, and the only
# thing in the way is a reseed interval meant to stop urandom draining
# /dev/random on a machine that has a scarce entropy source.  A QEMU guest with
# virtio-rng does not have a scarce source.  The interval is dropped for the
# seeding burst and then left at 1s, so the 60s wall cannot recur while a rate
# limit still exists.
#
# The knob is gone from 4.8 onwards (the CRNG replaced the two-pool design, and
# 5.4 added jitter entropy), so on every other tree here this script finds no
# file and does nothing.  Ordered S00 to precede seedrng (S01) and sshd (S50).
# Carry GLIBC_TUNABLES past busybox init.
#
# The kernel hands an unrecognised key=value on its command line to init as an
# environment variable, and v4.6-arm64 uses that to set
# GLIBC_TUNABLES=glibc.pthread.rseq=0 -- a 2026 glibc registers rseq(2) in every
# process, a 2016 kernel has no such syscall, and arm64's do_ni_syscall answers
# each one with a full register dump to the console.
#
# Measured, not assumed: /proc/1/environ DOES carry it, but a login shell's
# $GLIBC_TUNABLES is empty and a child of that shell has none either -- busybox
# init builds its children a fresh environment rather than passing its own on.
# So the command line covers PID 1 and nothing else, and the boot services that
# actually spawn the processes never see it.
#
# rcS is where every boot service is started, and /etc/profile where every login
# shell begins, so exporting there is what makes the setting reach the processes
# that would otherwise trigger the dumps.  Both are conditional on the value
# already being in init's environment: a tree that did not ask for this gets
# nothing, and no name is hardcoded in the guest.
RCS="$TARGET/etc/init.d/rcS"
if [ -f "$RCS" ] && ! grep -q 'GLIBC_TUNABLES' "$RCS"; then
    sed -i '1{/^#!/!b}; 1a\
\
# Passed to init on the kernel command line; busybox init does not hand its own\
# environment to its children, so re-export it here for every service rcS starts.\
if [ -r /proc/1/environ ]; then\
    _t=$(tr "\\0" "\\n" < /proc/1/environ | sed -n "s/^GLIBC_TUNABLES=//p")\
    [ -n "$_t" ] \&\& export GLIBC_TUNABLES="$_t"\
    unset _t\
fi' "$RCS"
fi
mkdir -p "$TARGET/etc/profile.d"
cat > "$TARGET/etc/profile.d/kbl-tunables.sh" <<'TEOF'
# Same reason as in rcS: busybox init does not pass its environment on, so a
# login shell starts without whatever the kernel command line gave init.
if [ -r /proc/1/environ ]; then
    _t=$(tr '\0' '\n' < /proc/1/environ | sed -n 's/^GLIBC_TUNABLES=//p')
    [ -n "$_t" ] && export GLIBC_TUNABLES="$_t"
    unset _t
fi
TEOF
chmod 0644 "$TARGET/etc/profile.d/kbl-tunables.sh"

ENTROPY="$TARGET/etc/init.d/S00entropy"
cat > "$ENTROPY" <<'EEOF'
#!/bin/sh
# Seed the urandom pool before anything calls getrandom(). No-op on kernels >= 4.8.
GATE=/proc/sys/kernel/random/urandom_min_reseed_secs

# Has the CRNG come up?  Asked directly, because the kernel says so, and both
# spellings are accepted: pre-4.8 prints "random: nonblocking pool is
# initialized" and 4.8 onwards prints "random: crng init done".  Inferring it
# from entropy_avail would be wrong on exactly the kernels this script exists
# for -- that counts the INPUT pool, which reads "plenty" all through the stall.
crng_up() {
    dmesg 2>/dev/null | grep -qE "nonblocking pool is initialized|crng init done"
}

start() {
    # The knob is not the version test.  It is still present, as a no-op kept for
    # ABI compatibility, long after the two-pool design it belonged to was
    # replaced -- so testing for it alone ran the whole loop on a modern kernel
    # and then reported failure at something that had already succeeded.
    [ -w "$GATE" ] || return 0
    crng_up && return 0                 # already up: nothing to do, cost nothing
    old=$(cat "$GATE" 2>/dev/null)
    echo 0 > "$GATE" 2>/dev/null || return 0
    # Each read pulls from the input pool; a couple of crossings put the
    # nonblocking pool over its 128-bit initialisation threshold.  The loop stops
    # the moment that happens -- normally after one or two reads -- so this costs
    # a fraction of a second, not its whole budget.  It is bounded because it must
    # never be able to hold up boot: if the entropy is not coming, this gives up
    # and the guest boots the slow way it did before.
    i=0
    while [ "$i" -lt 12 ]; do
        dd if=/dev/urandom of=/dev/null bs=256 count=1 2>/dev/null
        crng_up && break
        i=$((i + 1))
        sleep 0.1 2>/dev/null || break
    done
    # Restore a rate limit, but not the 60s one that caused the stall: a later
    # getrandom() must not be able to hit that wall either.
    echo 1 > "$GATE" 2>/dev/null || echo "${old:-60}" > "$GATE" 2>/dev/null
    if crng_up; then
        echo "entropy: urandom pool seeded after $((i + 1)) read(s) (reseed interval ${old:-?}s -> 1s)"
    else
        echo "entropy: urandom pool still uninitialised after 12 reads; boot continues"
    fi
}

case "$1" in
    start|"") start ;;
    stop)     : ;;
    restart)  start ;;
    *)        echo "usage: $0 {start|stop|restart}"; exit 1 ;;
esac
EEOF
chmod 0755 "$ENTROPY"

WINSIZE="$TARGET/etc/init.d/S12winsize"
cat > "$WINSIZE" <<'WEOF'
#!/bin/sh
NAME=org.kbuildlab.winsize
PIDFILE=/var/run/kbl-winsize.pid

port() {
    for n in /sys/class/virtio-ports/*/name; do
        [ -r "$n" ] || continue
        [ "$(cat "$n" 2>/dev/null)" = "$NAME" ] || continue
        d=${n%/name}
        [ -c "/dev/${d##*/}" ] && { echo "/dev/${d##*/}"; return 0; }
    done
    return 1
}

console() {
    c=
    for w in $(cat /proc/cmdline); do
        case "$w" in console=*) c=${w#console=}; c=${c%%,*} ;; esac
    done
    [ -n "$c" ] && [ -c "/dev/$c" ] && echo "/dev/$c"
}

serve() {
    exec 3<>"$1"
    while echo "?" >&3; do
        while read -r rows cols rest <&3; do
            case "$rows$cols" in ""|*[!0-9]*) continue ;; esac
            [ "$rows" -gt 0 ] && [ "$cols" -gt 0 ] || continue
            stty -F "$2" rows "$rows" cols "$cols" 2>/dev/null
        done
    done
}

start() {
    dev=$(port) || return 0
    tty=$(console) || return 0
    serve "$dev" "$tty" </dev/null >/dev/null 2>&1 &
    echo $! > "$PIDFILE"
}

stop() {
    [ -r "$PIDFILE" ] && kill "$(cat "$PIDFILE")" 2>/dev/null
    rm -f "$PIDFILE"
    return 0
}

case "$1" in
    start|"") start ;;
    stop)     stop ;;
    restart)  stop; start ;;
    *)        echo "usage: $0 {start|stop|restart}"; exit 1 ;;
esac
WEOF
chmod 0755 "$WINSIZE"

FSTAB="$TARGET/etc/fstab"
if [ -f "$FSTAB" ]; then
    grep -q '[[:space:]]/sys/kernel/debug[[:space:]]' "$FSTAB" \
        || printf 'debugfs\t\t/sys/kernel/debug\tdebugfs\tdefaults\t0\t0\n' >> "$FSTAB"
    grep -q '[[:space:]]/sys/kernel/tracing[[:space:]]' "$FSTAB" \
        || printf 'tracefs\t\t/sys/kernel/tracing\ttracefs\tdefaults\t0\t0\n' >> "$FSTAB"
fi

# Disk boots carry the kernel's modules archive on the boot partition (partition 1
# of the disk holding the root file system) as /modules.cpio.gz, written there by
# kbuildlab with the kernel; the initramfs path appends it to the cpio instead.
# S09 installs it onto the root disk, ahead of the archive's S10kbuildlab-coldplug.
MODULES="$TARGET/etc/init.d/S09kbl-modules"
cat > "$MODULES" <<'MEOF'
#!/bin/sh
# Install the boot partition's modules archive, once per distinct archive. Silent
# without one (initramfs boots, a boot partition that has none).
SUM=/lib/modules/.kbl-boot.sha256
HOOK=etc/init.d/S10kbuildlab-coldplug
MNT=/run/kbl-boot

root_in_ram() {
    t=
    while read -r _ mp fs _; do [ "$mp" = / ] && t=$fs; done < /proc/mounts
    case "$t" in rootfs|ramfs|tmpfs) return 0 ;; esac
    return 1
}

# Partition 1 of the disk the root file system is on, found from its device number.
bootpart() {
    mm=$(mountpoint -d / 2>/dev/null) || return 1
    part=$(readlink -f "/sys/dev/block/$mm") || return 1
    for p in "${part%/*}"/*/partition; do
        [ -r "$p" ] && [ "$(cat "$p")" = 1 ] || continue
        d=${p%/partition}; echo "/dev/${d##*/}"; return 0
    done
    return 1
}

start() {
    root_in_ram && return 0
    dev=$(bootpart) || return 0
    mkdir -p "$MNT"
    mount -t vfat -o ro "$dev" "$MNT" 2>/dev/null || return 0
    arc="$MNT/modules.cpio.gz"
    [ -f "$arc" ] || { umount "$MNT"; return 0; }
    sum=$(sha256sum < "$arc"); sum=${sum%% *}
    [ "$(cat "$SUM" 2>/dev/null)" = "$sum" ] && { umount "$MNT"; return 0; }
    # rcS took its S??* list before this ran, so it runs the hook only if it was there.
    rcs_hook=no
    [ -f "/$HOOK" ] && [ "$(cat "/proc/$PPID/comm" 2>/dev/null)" = rcS ] && rcs_hook=yes
    echo "kbl-modules: installing $(wc -c < "$arc") bytes from $dev"
    list=$(zcat "$arc" | busybox cpio -t 2>/dev/null | sed 's|^\./||; s|^/||')
    if ! err=$(cd / && zcat "$arc" | busybox cpio -idmu -R 0:0 2>&1); then
        echo "kbl-modules: could not extract the archive: $err"; umount "$MNT"; return 1
    fi
    umount "$MNT"
    kvers=$(printf '%s\n' "$list" | sed -n 's|^lib/modules/\([^/]*\)/.*|\1|p' | sort -u)
    # kmod's modprobe reads only modules.dep.bin, so that must have come along too.
    for kver in $kvers; do
        if printf '%s\n' "$list" | grep -qxF "lib/modules/$kver/modules.dep" &&
           printf '%s\n' "$list" | grep -qxF "lib/modules/$kver/modules.dep.bin"; then
            continue
        fi
        depmod "$kver" || echo "kbl-modules: depmod $kver failed"
    done
    echo "$sum" > "$SUM"
    echo "kbl-modules: installed modules for" $kvers
    if [ "$rcs_hook" = no ] && printf '%s\n' "$list" | grep -qxF "$HOOK"; then
        sh "/$HOOK" start
    fi
}

case "$1" in
    start|"") start ;;
    stop)     : ;;
    restart)  start ;;
    *)        echo "usage: $0 {start|stop|restart}"; exit 1 ;;
esac
MEOF
chmod 0755 "$MODULES"

# The login getty follows the kernel console (ttyS0, ttyAMA0, hvc0, ttymxc1,
# ttySIF0, ...) instead of BR2_TARGET_GENERIC_GETTY_PORT. Its inittab entry names
# no tty, so busybox init never respawns on a missing one; getty opens the real tty
# and takes it as controlling terminal, which job control and Ctrl-C need.
GETTY="$TARGET/sbin/kbl-getty"
cat > "$GETTY" <<'GEOF'
#!/bin/sh
# The last console= wins, as for /dev/console; else the kernel's pick. Idle if none.
usable() { [ -n "$1" ] && [ -e "/sys/class/tty/$1" ] && [ -c "/dev/$1" ]; }
tty=
for w in $(cat /proc/cmdline); do
    case "$w" in console=*) tty=${w#console=}; tty=${tty%%,*} ;; esac
done
if ! usable "$tty"; then
    for t in $(cat /sys/class/tty/console/active 2>/dev/null); do tty=$t; done
fi
[ "$tty" = tty0 ] && tty=tty1
usable "$tty" || exec sleep 2147483647
exec /sbin/getty -L "$tty" 0 xterm-256color
GEOF
chmod 0755 "$GETTY"
INITTAB="$TARGET/etc/inittab"
if [ -f "$INITTAB" ]; then
    sed -i 's~^.*# GENERIC_SERIAL$~::respawn:/sbin/kbl-getty # GENERIC_SERIAL~' "$INITTAB"
fi

# eudev's 80-net-name-slot.rules would rename eth0 (enp0s1 and the like) during its
# coldplug, before S40network brings up BR2_SYSTEM_DHCP's eth0. Keep kernel names.
if [ -d "$TARGET/lib/udev/rules.d" ]; then
    mkdir -p "$TARGET/etc/udev/rules.d"
    for r in "$TARGET"/lib/udev/rules.d/*net-name*.rules; do
        if [ -e "$r" ]; then ln -sf /dev/null "$TARGET/etc/udev/rules.d/${r##*/}"; fi
    done
fi
