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
tag, rebuilds with **the .config you already had**, and refreshes ctags and
cscope. It never re-applies the preset, because your local answers to new
Kconfig symbols are yours.

## The debuggability preset

`presets/` holds a kernel configuration for one purpose: seeing as much as the
kernel can be made to show. It is split three ways because a naive shared/
per-arch split fails on a specific class of option.

| file | what it holds |
| --- | --- |
| `common.config` | what all three architectures can set identically |
| `arch/<arch>.config` | what looks shared but is not, plus each architecture's own |
| `verify.sh` | reads the built `.config` back and fails on anything that did not survive |

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

- **riscv64 has no hardware breakpoints.** `HAVE_HW_BREAKPOINT` is absent, so
  gdb `watch` falls back to single-stepping the whole kernel and
  `perf record -e mem:...` is refused.
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
symlink for tools that look for that name first. `kbuildlab` probes for a
runtime in that order, or takes `$KBL_CONTAINER`.

## Dependencies

Required: a container runtime, `qemu-system-<arch>`, and a gdb that can debug
the target — a cross gdb, or a host gdb built `--enable-targets=all`.

Optional: [gdbtools](https://github.com/yugeun-song/gdbtools), which is what
makes symbols resolve before the MMU is on. `kbuildlab attach` finds it if it is
installed and says so if it is not.

Nothing is installed for you, and no system setting is changed.
