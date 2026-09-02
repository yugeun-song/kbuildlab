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
  --port|-g N                gdb port (default: the tree's GDB_PORT); --mem|-m, --smp, --bios

kbuildlab attach [tree] [options] [-- GDB ARGS]
  --no-bootbreak|--raw       attach to an already-booted guest, no run-to-_text
  --stop text|firmware|start_kernel   where to stop (default text = head.S _text)
  --port|-p N                gdb port (default: the tree's GDB_PORT)
```

The gdb-port flag differs by command: `run` names it `-g` (QEMU's own spelling),
`attach` names it `-p`.

## The tree.conf

`kbuildlab init NAME --arch ARCH` writes `<workspace>/NAME/tree.conf` from
`templates/` -- the machine description every command reads, never inferred from
the directory name. It states what the tree is (`NAME`, `ARCH`, `VERSION`,
`UPSTREAM`) and the facts `run` and `attach` consume: `QEMU_BIN`, `MACHINE`,
`CPU`, `CONSOLE`, `KERNEL_IMAGE_REL`, `GDB_PORT`, and where they apply `BOOT`,
`INITRD`, `PERSIST`, `CPU_PAGING` and the firmware paths (`UBOOT`,
`OVMF_CODE`/`OVMF_VARS`, `UEFI_ENTRY`). `attach` also hands the debugger every
`GDBTOOLS_*` line verbatim -- e.g. `GDBTOOLS_ENTRY_PA`, a kernel image base
pinned for a target that cannot report it. A `qemu.conf` symlink points at the
same file, which is what the editor's debug adapter reads.

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
the order docker, podman, nerdctl -- buildah reads the `Containerfile` but cannot
run a guest, so it is not auto-selected -- or takes `$KBL_CONTAINER`, which wins.

`kbuildlab image` builds the default Ubuntu-24.04 image; `kbuildlab image legacy`
builds a gcc-6 image (`containers/Containerfile.legacy`) for old kernels such as
v4.6.

## Dependencies

Required: a container runtime, `qemu-system-<arch>`, and a gdb that can debug
the target — a cross gdb, or a host gdb built `--enable-targets=all`.

Optional: [gdbtools](https://github.com/yugeun-song/gdbtools), which is what
makes symbols resolve before the MMU is on. `kbuildlab attach` finds it if it is
installed and says so if it is not.

Nothing is installed for you, and no system setting is changed.
