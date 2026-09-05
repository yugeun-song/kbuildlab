# kbuildlab

Build a Linux kernel in a container, boot it in QEMU, debug it with gdb.

The container is the point. A rolling-release host moves its toolchain whenever
it likes, and while the kernel itself usually survives that, buildroot and the
rest of the userspace tooling do not. Pinning the build environment to a stable
base separates *what my machine has today* from *what this kernel was built
with*, and makes a tree reproducible on a different machine a year later.

Targets **arm64**, **riscv64** and **x86_64**.

## Install

```sh
git clone https://github.com/yugeun-song/kbuildlab
kbuildlab/bin/kbuildlab doctor          # what is missing, by name
```

Put `bin/kbuildlab` on your PATH however you prefer. Nothing here installs
packages, edits sysctls or touches anything outside the workspace you name: if a
prerequisite is absent, the tool says which one and stops.

## Use

```sh
kbuildlab init --workspace ~/kernels    # where trees live, recorded once
kbuildlab init arm64-v6.12 --arch arm64 # a tree; --version defaults to v6.12
kbuildlab image                         # build the container once

kbuildlab sync   arm64-v6.12            # fetch the source
kbuildlab config arm64-v6.12 --preset   # defconfig + the debuggability preset
kbuildlab build  arm64-v6.12
kbuildlab run    arm64-v6.12            # boot, frozen at the reset vector
kbuildlab attach arm64-v6.12            # gdb, in another terminal
```

`kbuildlab update <tree>` is the one for a tree you already have: it fetches,
fast-forwards if the tracked version is a moving branch rather than a pinned
tag, rebuilds with **the .config you already had**, and refreshes ctags,
cscope and -- when the build image ships GNU GLOBAL -- gtags. It never
re-applies the preset, because your local answers to new Kconfig symbols are
yours.

Every command takes `-h`/`--help`; `run` and `attach` carry the options worth
knowing.

```
kbuildlab run [tree] [options] [-- QEMU ARGS]
  --run                      boot now instead of freezing at the reset vector
  --boot direct|uboot|uefi   boot path (default: the tree's BOOT, else direct)
  --kaslr | --no-kaslr       randomized vs deterministic base (default --no-kaslr)
  --kvm | --no-kvm           x86 only; TCG by default, for deterministic early-boot HW breakpoints
  --initrd PATH | --no-initrd
  --net | --no-net           user NIC with an ssh forward (default on); --ssh-port N
  --persist | --no-persist   attach the tree's writable /persist disk
  --append WORDS             extra kernel command-line words, repeatable
  --port|-g N                gdb port (default: the tree's GDB_PORT); --mem|-m, --smp, --bios

kbuildlab attach [tree] [options] [-- GDB ARGS]
  --no-bootbreak|--raw       attach to an already-booted guest, no run-to-_text
  --stop text|firmware|start_kernel   where to stop (default text = head.S _text)
  --port|-p N                attach by port; the tree comes from the guest's run state
  --list|-l                  list live guests and exit; never prompts
  --first                    with several matches take the lowest port (for scripts)
```

The gdb-port flag differs by command: `run` names it `-g` (QEMU's own spelling),
`attach` names it `-p`.

`attach` puts gdb's working directory in the kernel source, because the kernel's
own `scripts/gdb` commands need it there -- `lx-symbols` reloads the image with a
bare `symbol-file vmlinux`, and `lx-dmesg`/`lx-lsmod` resolve module paths the
same relative way. Arguments you pass after `--` are unaffected: a relative path
in the positions gdb's syntax defines as paths (`-x`, `--command`, `-s`, `-c`,
`-d`, `-e`, and the first positional) is made absolute against the directory you
typed the command in first, and `set logging` writes there too. Only the
interactive session that follows sees the kernel tree as its cwd.

### Several guests of one tree

`run` advances to a free port rather than refusing, so a second guest of the same
tree is normal -- and its port is a number that appears nowhere in `tree.conf`.
`attach` therefore discovers rather than assumes:

- one live guest of that tree -> exactly as before, no prompt and no extra output;
- several -> a chooser (fzf when present, else a numbered prompt), or a refusal
  naming the ports when stdin is not a terminal;
- `--port N` alone -> the tree is recovered from that guest's own run state, so
  the right `vmlinux` is still loaded;
- both, disagreeing -> refused, saying what is actually on that port.

The tree's `GDB_PORT` stays `run`'s default and is no longer `attach`'s: once a
second guest exists, that number identifies one of them at best.

The listing says whether each guest already has a debugger, and works that out
**without touching the gdbstub**. A bare TCP connect -- not one RSP byte -- pauses
a running guest and leaves it paused after the socket closes (measured on qemu
11.1.1 through QMP `query-status`), so a probe that asked by connecting would stop
the guest it was asked about. The answer instead comes from `/proc`: the socket
inodes qemu's own fds hold, intersected with the ESTABLISHED rows on that port in
`/proc/net/tcp` and `/proc/net/tcp6`. Ownership is the point -- the stub keeps
listening after it accepts, so a second client finishes its handshake and shows as
ESTABLISHED while queued behind the first, and counting rows alone would call that
attached. An unreadable fd table (another user's process) reads `unknown`, never a
guess.

```
$ kbuildlab attach v4.6-arm64 --list
live guests of v4.6-arm64:
  #   port   ssh    tree             boot    kaslr  state    dbg          pid      uptime
  1   1435   2222   v4.6-arm64       uboot   off    frozen   -            86359    00:41
  2   14991  2223   v4.6-arm64       uboot   off    running  attached     89478    00:12
```

The stub serves exactly one client, and a second `attach` on the same port does
not usefully queue: the kernel completes the handshake, qemu never accepts that
socket, and the packets sit unread until gdb's timeout -- after which, if the
first client leaves, the late replies arrive one packet out of step and the
session fails with a protocol error. `attach` says so rather than appearing to
hang. Start a second guest instead; that is what the port allocator is for.

### Choosing a port

`run` claims its port by creating the run-state file with `O_EXCL`, and the
claim IS the test: an unclaimed, unlistened port is taken by whoever creates the
file first, and a run that loses moves to the next number. Testing for a free
port and claiming it later would leave a window -- and it is not a small one,
since a firmware chain spends seconds on `mkimage`, `mcopy` and a 90-128 MB copy
before anything listens. Two runs started together used to land on the same port
and the loser's cleanup then deleted the winner's live boot disk. Cleanup now
removes the state file only while it still names this launcher.

An explicit `--port` has nowhere to move to and is refused if the port is taken.

### The run state

`run` records what it started in `$KBL_STATE_DIR/kbl-run-<port>.env` (default
`/dev/shm`), written whole and renamed into place so a reader never sees half a
record. `attach` and the editor adapter both read it, so its `KBL_*` key format is
a contract: keys are added, never renamed. It carries the boot mode, the load
address a firmware chain landed the kernel at, the KASLR state, the ssh port, the
effective kernel command line -- which a firmware chain passes itself and so does
not appear on qemu's own command line -- and qemu's pid together with its
`/proc` start time, which is what lets a file that outlived its guest be told
apart from one describing the process actually on that port.

## The tree.conf

`kbuildlab init NAME --arch ARCH` writes `<workspace>/NAME/tree.conf` from
`templates/` -- the machine description every command reads, never inferred from
the directory name. It states what the tree is (`NAME`, `ARCH`, `VERSION`,
`UPSTREAM`) and the facts `run` and `attach` consume: `QEMU_BIN`, `MACHINE`,
`CPU`, `CONSOLE`, `KERNEL_IMAGE_REL`, `GDB_PORT`, and where they apply `BOOT`,
`INITRD`, `PERSIST`, `CPU_PAGING` and the firmware paths (`UBOOT`,
`OVMF_CODE`/`OVMF_VARS`, `UEFI_ENTRY`), plus two that steer the guest itself:
`CMDLINE_EXTRA` (words appended to the kernel command line in every boot mode --
`-append` for a direct boot, `setenv bootargs` in the u-boot script, the `linux`
line in `grub.cfg`) and `GDB_BIND` (the address the gdbstub listens on, default
`127.0.0.1`, because a gdbstub is an unauthenticated read/write channel into guest
memory and qemu's own `tcp::N` spelling binds every interface).

Every value in it is read with ONE grammar, and the same one in all five parsers
that read the file -- kbuildlab's `kbl_tree_get`, its `GDBTOOLS_*` passthrough, the
two firmware scripts, and the editor adapter's `discover.lua`:
`^\s*KEY\s*=\s*VALUE`, trailing `#` comment and space stripped, one layer of
surrounding double quotes removed. They used to differ, and a line written
`GDBTOOLS_ENTRY_PA = 0x40200000` was honoured by the editor and dropped in silence
by the terminal, so one guest calibrated to two different addresses.

`attach` also hands the debugger every
`GDBTOOLS_*` line verbatim, with one exception: `GDBTOOLS_ENTRY_PA` -- a kernel
image base pinned for a target that cannot report it -- describes the mode the
tree DEFAULTS to, so it is applied only when that is the mode this port is
actually running, and x86 KASLR takes it back again because the base moves per
run. A `qemu.conf` symlink points at the same file, for older editor configs
that looked for that name; the debug adapter reads `tree.conf` itself.

Per-machine settings live outside the repo. `kbuildlab init --workspace DIR`
records the workspace path in `~/.config/kbuildlab/workspace`, which is never
committed; `$KBL_WORKSPACE` overrides it. Everything else per machine is a
`KBL_*` variable (`KBL_CONTAINER`, `KBL_IMAGE`, `KBL_JOBS`, `KBL_DOCKER_USER`,
...), read from the environment -- keep them in an untracked file your shell
sources, not in the tree.

## The debuggability preset

`presets/` holds a kernel configuration for one purpose: seeing as much as the
kernel can be made to show. It is split three ways because a naive shared/
per-arch split fails on a specific class of option.

| file | what it holds |
| --- | --- |
| `common.config` | what all three architectures can set identically |
| `arch/<arch>.config` | what looks shared but is not, plus each architecture's own |
| `verify.sh` | reads the built `.config` back and fails on anything that did not survive |

When a tree tracks `UPSTREAM=mainline`, `arch/<arch>-mainline.config` stands in
for `arch/<arch>.config` where mainline has moved past the pinned release --
riscv64 already ships one.

Every line was checked against the 6.12 source. Promptless symbols are
deliberately absent: `make olddefconfig` recomputes them from their selecters,
so a hand-written line for one is discarded and the fragment quietly claims
something it did not do.

`verify.sh` exists for the same reason. A Kconfig fragment whose `depends on` is
unmet is not rejected — it is dropped, the build succeeds, and the feature is
missing at runtime. `kbuildlab config --preset` runs the check and fails loudly
instead.

### What it buys

- **Debug information kept whole.** DWARF5, not reduced, not split, not
  compressed. `CONFIG_DEBUG_INFO_REDUCED` in particular adds `-fno-var-tracking`,
  which stops the compiler describing where a variable lives after optimisation —
  it is the direct cause of `<optimized out>` on locals that were otherwise
  recoverable.
- **Symbols kept whole.** `KALLSYMS_ALL`, no `STRIP_ASM_SYMS`, no
  `TRIM_UNUSED_KSYMS`, a linker map, and BTF for drgn, bpftrace and CO-RE.
- **Tracing.** ftrace and function_graph with return values, the latency
  tracers, histogram and synthetic events, kprobes and uprobes with BTF-named
  arguments, blktrace, perf. Enough for ftrace, trace-cmd, kernelshark,
  bpftrace, uftrace, blktrace and `perf trace`.
- **eBPF and XDP.** Syscall, JIT, LSM, BTF, and the attach points the tooling
  expects.

### What it does not

The kernel cannot be built at `-O0` or `-Og`; 6.12 offers `-O2` or `-Os` and
nothing else. At `-O2`, with everything above:

- locals whose lifetime has ended have had their registers reused, and gdb
  saying `<optimized out>` there is accurate rather than a defect
- `static inline` in headers is gone from `nm`, `readelf`, kallsyms, ftrace and
  BTF alike; it survives only as `DW_TAG_inlined_subroutine` in DWARF
- `.isra.N` clones survive — no flag the kernel offers removes them
- hand-written assembly entry paths keep no frame-pointer discipline, so a
  backtrace still ends at the kernel entry boundary

Self-tests and profilers are off on purpose. `FTRACE_STARTUP_TEST` exercises
every configured tracer on every boot and pulls `EVENT_TRACE_STARTUP_TEST` with
it; `RING_BUFFER_STARTUP_TEST` promises ten seconds and disables every ring
buffer if it dislikes what it sees. "As many features as possible" means the
capability is present and idle, not that every diagnostic is armed.

### Architecture differences that are not preferences

- **riscv64 has no *in-guest* hardware breakpoints.** `HAVE_HW_BREAKPOINT` is
  absent, so the guest's own `perf record -e mem:...` is refused and an in-guest
  ptrace watchpoint has no debug register to program. Debugging over the QEMU
  gdbstub is a separate path and is unaffected -- QEMU enforces the watchpoint in
  the emulator, so `watch` (and gdbtools' `kw`) from `kbuildlab attach` stop the
  guest the instant the address is written, not by single-stepping.
- **riscv64 ftrace and preemption are mutually exclusive.** `HAVE_FUNCTION_TRACER`
  is selected `if !XIP_KERNEL && !PREEMPTION`, so a preemptible riscv64 kernel
  has no ftrace at all — and Kconfig does not warn, it simply stops offering it.
- **arm64 and riscv64 have no `DYNAMIC_FTRACE_WITH_REGS`**, so `FPROBE`,
  `KPROBES_ON_FTRACE` and BPF `kprobe_multi` are unreachable on both.
- **arm64 loses BPF trampolines under CFI.** `DYNAMIC_FTRACE_WITH_DIRECT_CALLS`
  sits behind `DYNAMIC_FTRACE_WITH_CALL_OPS` behind `!CFI_CLANG`.

Each arch fragment states these where you will read them.

## Containers

`containers/Containerfile` is plain OCI with no builder extensions, so **docker,
podman, buildah and nerdctl all read it unchanged**. `Dockerfile` beside it is a
symlink for tools that look for that name first. `kbuildlab` picks a runtime in
the order podman, docker, nerdctl -- buildah reads the `Containerfile` but cannot
run a guest, so it is not auto-selected -- or takes `$KBL_CONTAINER`, which wins.
podman comes first because rootless podman needs no daemon: a machine that keeps
dockerd off builds without starting a service or asking for root.

`kbuildlab image` builds the default Ubuntu-26.04 image. `kbuildlab image legacy`
builds an Ubuntu-24.04 image (`containers/Containerfile.legacy`) carrying gcc-9
through gcc-13; **no tree here uses it**. It is kept for two reasons: it is where
the unaided ceiling below was measured, and it is the answer for anyone who would
rather build a 2016 kernel with an era-appropriate compiler than with a 2026 one
plus a flag. `containers/Containerfile.legacy-gcc6` goes further back still --
Debian 9, gcc-6.3 -- and is the same kind of fallback.

Each image carries **several compiler generations, not one** -- 14, 15 and 16 in
the default image, 9 through 13 in the legacy one, each with the aarch64 and
riscv64 cross compilers to match. A tree names the one it builds with, in its own
`tree.conf`:

```
IMAGE=kbuildlab-legacy:latest   # which image; unset means the default one
GCC=16                          # which compiler in it; unset means the image's own gcc
HOSTCC_FLAGS=-fcommon           # appended to HOSTCC only; usually unset
KCFLAGS=-fvar-tracking-assignments   # appended after the kernel's own CFLAGS
```

The reason for the range is that *"the newest toolchain"* is not one answer across
seven kernels. gcc-10 turned `-fno-common` on by default, and a kernel from before
5.6 has tentative definitions that then collide at link -- v4.6 stops in its own
host tools, `scripts/dtc`, on a duplicate `yylloc`. Mainline tracks compiler trunk;
a stable series was validated against what shipped with it. So the ceiling belongs
to the tree, is found by building rather than assumed, and is written where the
rest of that tree's build is described instead of in an environment variable
somebody has to remember. `kbuildlab build` reports which compiler it used.

`HOSTCC_FLAGS` is the narrow escape hatch for exactly that `-fno-common` case: it
is appended to `HOSTCC`, not set as `HOSTCFLAGS` (which would *replace* the
kernel's own `-O2` and warning set rather than add to it), and it reaches the
host tools only. That distinction is the whole reason it is acceptable, and it
turns out to be worth a lot: **v4.6's ceiling is gcc-9 unaided and gcc-16 with
`-fcommon` on the host compiler**, seven generations apart, with the kernel's own
code generation identical either way. It also decides that tree's debug-info
format, since v4.6 has no Kconfig choice for one and gcc-11 and later default to
DWARF5. Use `HOSTCC_FLAGS` for that; a flag that changes the kernel's own objects
belongs in the config, where the read-back check can see it.

`KCFLAGS` is the kernel's own append-to-`KBUILD_CFLAGS` variable, so what is put
there lands *after* everything the tree's Makefile set and wins where the two
conflict. That is how a flag the kernel hard-codes gets undone without editing
the kernel. v4.6 is the case that needs it: its `Makefile:719` adds
`-fno-var-tracking-assignments` unconditionally, a 2013 decision about gcc build
times, and variable-tracking assignments are most of what keeps an optimised
local from printing as `<optimized out>`. Keep this to flags that change debug
information only -- gcc guarantees those do not change code generation -- because
a flag that changes the objects belongs in the config, where the read-back check
can see it.

Measured ceilings, one build per rung, newest first:

| tree | builds with | limited by |
|---|---|---|
| `upstream-*` (7.3-rc1) | gcc-16 | nothing found |
| `v6.12-*` | gcc-16 | nothing found |
| `v4.6-arm64` | gcc-16 + `HOSTCC_FLAGS=-fcommon` | host tools only; gcc-9 unaided |

gcc-16 is a development snapshot rather than a release. Every tree here names it
explicitly, which is the point of the pin being per tree: moving one back is
editing one line of its `tree.conf`, and `kbuildlab containers` then reports the
tree as `MIXED` until it is rebuilt.

The unversioned `gcc` in the default image is **15**, the newest release 26.04
offers; **16** is installed but is a dated development snapshot
(`16-2026MMDD`, no `.0`), so a tree opting into it is opting into a prerelease
compiler deliberately. On the legacy image only the *cross* names are repointed --
`aarch64-linux-gnu-gcc` is **9**, the highest rung v4.6 builds on unaided -- while
the native `gcc` there stays 24.04's own **13**, which is what builds the host tools.

Only the compiler is versioned. `CROSS_COMPILE` still supplies `ld`, `as` and
`objcopy`, which the distribution ships in one version per target, and `HOSTCC`
moves with `CC` so the host tools that parse the kernel's own output are not a
generation apart from it.

The note at the top of the `Containerfile` is not idle -- buildroot breaks on new
compilers well before the kernel does -- so a failing rootfs build gets
`HOSTCC=gcc-14` rather than the whole image moved back.

`kbuildlab containers` reports what the runtime holds for kbuildlab and the four
ways an Ubuntu container on a host with its own package manager can collide with
it, each checked rather than assumed:

- **The environment.** A build must not depend on the shell it was started from,
  so the image is asked what it actually receives and the answer is compared
  against the variables that would steer a kernel build (`CC`, `CFLAGS`,
  `MAKEFLAGS`, `CROSS_COMPILE`, `LD_LIBRARY_PATH` and the rest). Both runtimes
  start a container with a fresh environment, so the expected answer is *clean*;
  this says so from the container's own `env`, not from the documentation.
- **The toolchain.** The host compiles with its distribution's gcc and the image
  with Ubuntu's, and one build tree cannot hold objects from both. Every tree's
  recorded `CONFIG_CC_VERSION_TEXT` is compared against the compiler that would
  build it *now* -- per tree, not per architecture, because a tree names its own
  image and its own compiler version; asking the default image's native gcc about
  a tree pinned to another one compares two unrelated compilers and reports a
  mismatch that means nothing. A tree built by another compiler is reported as
  `MIXED` and wants `make mrproper` before it is built here again; a tree naming a
  compiler the image does not have is reported as `NO CC`, and one naming an image
  that is not built as `MISSING`. A tree older than 4.19 records no
  `CONFIG_CC_VERSION_TEXT` at all, so there is nothing to compare; it is reported
  as `unchecked` rather than skipped, because an unchecked thing that reads as a
  checked one is the failure this command exists to prevent.
  The `Containerfile`'s own default is checked against the image too -- the `ln`
  that makes `/usr/bin/gcc` point somewhere, not the newest package it installs,
  since the image carries several on purpose. Editing it without rebuilding is
  reported as `STALE`, because nothing else would say so.
- **The mount.** The workspace is bind-mounted at the same path it has on the
  host, so a workspace inside `/usr`, `/etc`, `/var` or any other system
  directory is refused outright rather than handed to a container that could
  write package-managed files underneath the package manager.
- **The host's own config.** `/etc/containers` is checked for unmerged `.pacnew`
  files, and rootless podman for the `subuid`/`subgid` ranges it needs. Files in
  the workspace owned by root -- the mark of a rootful run -- are reported too.

`kbuildlab containers clean` removes the containers made from kbuildlab's images
and nothing else. `--images` also drops the images kbuildlab built. `--dangling`
additionally removes untagged images, and says plainly that untagged images
belong to no project by name, so that sweep is not limited to this one. Nothing
here ever prunes the runtime wholesale: it is shared with whatever else the
machine runs.

## Dependencies

Required: a container runtime, `qemu-system-<arch>`, and a gdb that can debug
the target — a cross gdb, or a host gdb built `--enable-targets=all`.

Optional: [gdbtools](https://github.com/yugeun-song/gdbtools), which is what
makes symbols resolve before the MMU is on. `kbuildlab attach` finds it if it is
installed and says so if it is not.

Nothing is installed for you, and no system setting is changed.
