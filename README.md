# kbuildlab

Build a Linux kernel in a container, boot it in QEMU, debug it with gdb or drgn.

The container pins the build environment to a stable base, so *what my machine has
today* stays separate from *what this kernel was built with* and a tree rebuilds
on another machine a year later. Targets **arm64**, **riscv64** and **x86_64**.

## Install

```sh
git clone https://github.com/yugeun-song/kbuildlab
kbuildlab/install.sh          # ~/.local/bin/kbuildlab; --system: /usr/local/bin
kbuildlab doctor              # what is missing, and the package that provides it
```

`install.sh` links the command into a directory on `PATH` (`--alias kbl` adds a
second name), puts its bash and zsh completions where bash-completion and zsh
look, and records the checkout in `~/.config/kbuildlab/root`, so an editor started
without that `PATH` still finds it. Run as yourself, `--system` asks sudo (else
doas or run0; `--sudo CMD` names another) for the links under `/usr/local` alone
and records the checkout as you; another user can follow those links only if they
can enter the checkout. zsh reads a user's completions from
`~/.local/share/zsh/site-functions` only once that is on `$fpath`, and
`install.sh` says how when it is not. It never edits shell files and installs no
package: `doctor` names what is missing with the install command for Arch,
Debian/Ubuntu, Fedora/RHEL, Void and openSUSE.

**Assumed:** Linux with `/proc`, bash 4.4, GNU coreutils, Python 3.12, git, one of
podman/docker/nerdctl, QEMU for each target (8.0 for riscv64's paging properties,
8.2 for `--emulate`'s virtio sound), and a gdb with a working Python. Outside
that, a command stops and names what is missing.

## Use

```sh
kbuildlab init --workspace ~/kernels               # rootfs, firmware and restores, recorded once
kbuildlab init ~/kernels/v6.18-arm64 --arch arm64  # register a tree in that directory;
                                                   # --version defaults to v6.18; prints its ID
kbuildlab image                                    # build the container once

kbuildlab sync   2349                              # fetch the source (2349: an ID prefix)
kbuildlab config 2349 --preset                     # defconfig + the debuggability preset
kbuildlab build  2349
kbuildlab run    2349                              # boot, frozen at the reset vector
kbuildlab attach 2349                              # gdb, in another terminal
kbuildlab drgn   2349                              # drgn, once the guest has booted
```

`kbuildlab update [TREE...|--all]` is the one pass over trees you already have:
it checks what the tree needs, syncs (fast-forwarding a tracked branch, never a
pinned tag), keeps **the .config you already had** (`olddefconfig`, then a report
of what changed and of any preset line the newer source dropped, repairing
neither), rebuilds incrementally with the modules, and refreshes ctags, cscope,
gtags and clangd (`--no-tags`, `--no-clangd`, `--no-warm`). Each tree runs on its
own, and a table at the end says which stages passed.

`kbuildlab help COMMAND`, or `COMMAND --help`, prints one command's options. Tab
lists what may come next: commands, options and their values, registered trees,
`tree.conf` keys and the ports of live guests.

## Trees

kbuildlab keeps a registry, the way a container runtime keeps images. Its own
records stay out of the kernel source; only what the kernel's `make` writes there
(the build, and the ctags, cscope and gtags indexes) lands in the tree:

```
~/.config/kbuildlab/trees/ID/tree.conf            what the tree is; edit it by hand
~/.local/share/kbuildlab/trees/ID/                boot disks, /persist, modules archive
```

Both follow `XDG_CONFIG_HOME` and `XDG_DATA_HOME`. Every command reads `tree.conf`
afresh, so an edit applies to the next command.

A tree has no name. It is known by its ID: 16 hex digits read from `/dev/urandom`
when it is registered, drawn again if a registered tree, a data directory or a
lab record already uses them, and never changed after, so moving the source keeps
it. The ID is shown as its first 12 digits, or as many more as it takes to differ
from every other tree's. `TREE` is an ID or any unique prefix of it with 4 or more
digits, or any path inside its source (or the directory holding it); omitted, it
is the tree the current directory belongs to, else the only one. A word that is
an existing path is read as the path, and an ambiguous prefix is an error. `rm`
and `mv` take an ID only, never a path. `init` takes the directory the source is
cloned into, and the directory name is only a path: two trees may use the same
one in different places.

```
kbuildlab trees [-q|--json|--narrow]        ID ARCH KERNEL REF COMMIT HASH BUILT GUESTS SOURCE
kbuildlab inspect TREE [--json|--format KEY]
kbuildlab add DIR [--arch A]                register a source you have (arch from .config)
kbuildlab get|set|unset TREE KEY...         read or write tree.conf keys; edit opens it
kbuildlab mv ID DIR [--no-move]             move the source and repoint SRC
kbuildlab rm ID [--purge] [--yes]           unregister; --purge also deletes the source
kbuildlab prune                             trees whose source is gone, orphaned data
```

The kernel version, commit, build time and running guests are read from the tree
and the host each time, never stored. `trees --narrow` prints each tree as a short
block instead of a table row, for a terminal narrower than the table; `-q` prints
the IDs only. `rm` and `mv` refuse while a guest of the tree runs; `rm --purge`
asks first (`--yes` for scripts). `SRC` is stored as an absolute path (`set
SRC=~/linux` expands it). `prune` keeps a tree whose source's parent is missing
too (a drive not mounted) or that was never built or run, and never deletes a data
directory holding a persistent disk.

### The lab/ record

Every `config` and `update` records the tree in this repository under
`lab/trees/ID/`: its `tree.conf`, any `config-fragment`, and its `.config` as
`savedefconfig` writes it (only what differs from the arch defconfig).
`lab/workspace/` keeps the workspace's own files (those at its top and at the top
of each directory a `tree.conf` points into, where the rootfs and firmware are
built) and `sources` (path, URL and commit of each non-kernel checkout, such as
buildroot and u-boot). A file over GitHub's 100 MiB limit is left out by name. A
record spells the workspace path as `@KBL_WORKSPACE@`, so it names no machine.

So the workspace can be deleted, or kbuildlab cloned onto another machine:

```sh
kbuildlab init --workspace ~/kernels --restore   # files, checkouts, every tree
kbuildlab sync ID && kbuildlab config ID         # config starts from the record
```

`init --restore` never overwrites a file that is there, and brings each tree back
under the ID it was recorded with. Commit `lab/` to keep it.

### ctags, cscope and gtags

None of the three evaluates `#if`, so run over the source as it is they index
every branch and every file the configuration never builds. `kbuildlab tags`
instead hands them the tree **as its build compiled it**: each compile a `.cmd`
file records is replayed with `-fdirectives-only`, a line no unit compiled is
blanked and a file no unit read is emptied (line numbers kept). The copies live in
`$XDG_CACHE_HOME/kbuildlab/view/<tree>/`; podman overlays them on the tree so the
kernel's own `make tags cscope gtags` run unchanged, and the finished indexes move
into the tree. cscope and gtags stay incremental; ctags is merged. Other runtimes
have no overlay volumes and index the tree as it is on disk, as `KBL_TAGS_VIEW=0`
does.

### The clangd index

`kbuildlab tags` builds a fourth index, for clangd, so an editor is not left
describing the previous build. It runs on the host (its input is the
`.cmd` files kbuild wrote, and the clangd that reads it is the host's) and writes
nothing into the tree: the database lives in
`$XDG_CACHE_HOME/kbuildlab/clangd/<tree>/`, and clangd is pointed there from
`~/.config/clangd/config.yaml`.

A GCC-built kernel is not a project clangd reads as-is. The generated config
closes each gap, measured against the tree rather than taken from a list:

- GCC-only flags (`-fconserve-stack`, `-mabi=lp64`, ...) are probed against clang
  for each unit's target and only the rejected ones dropped, so `-mabi=lp64` stays
  for riscv64 and goes for arm64.
- Each entry keeps the build compiler's own target (x86's i386 boot stub, riscv's
  32-bit vDSO, host tools) and, for a file built more than once, the reading most
  units share.
- Headers opened on their own get the includes that precede them in a real source
  (so `asm/atomic_lse.h` sees `atomic_t`), and their inactive `#if` regions come
  from the build, not from clang's own `__clang__`/`__GNUC__`/`__has_*` answers.
- Source the tree never compiles (`arch/powerpc` on an arm64 tree) keeps
  navigation and loses diagnostics; everything "unused" is off; `-ferror-limit=0`
  stops the fatal `too many errors` line surviving a silenced tree.

Nothing in the generator names a directory, header or macro of a particular tree:
the include paths, the arch directory, each unit's target and the compiler's own
headers are re-read on every run, so the next `kbuildlab tags` follows a config or
option change.

Two facts the build knows and clangd does not are injected as header comments:
go-to-definition on a `CONFIG_` symbol lands on its `#define` with Kconfig's
prompt, type, dependencies and help; on `_text` or `__init_begin`, on the
declaration with its `vmlinux.lds.S` line. clangd's hover card omits these — jump,
don't hover.

The index is built here rather than left to the editor, so it is finished before
the editor opens; clangd keys its shards on file contents, so a rebuild re-indexes
only the files that changed.

#### Editors

Everything above lives in `~/.config/clangd/config.yaml`, clangd's editor-neutral
user config, so the same blocks apply in Neovim, VS Code and on the command line;
an editor only supplies clangd's launch arguments. The per-tree blocks are
generated; the workspace-wide policy is `presets/clangd/workspace.yaml`, which you
edit and `kbuildlab tags` materialises under its own markers. Nothing in the
config is hand-written, so regenerating loses no decision.

VS Code's default C/C++ extension (`ms-vscode.cpptools`) runs its own parser,
ignores this file, and reports hundreds of phantom errors on a GCC-built kernel.
Install `llvm-vs-code-extensions.vscode-clangd` and merge
`presets/editor/vscode-settings.json` into your user settings; that switches
cpptools' parser off (its debugger is untouched).

```
kbuildlab tags [tree] [--no-clangd]     ctags, cscope, gtags, clangd + index
                      [--clangd-only]   just the clangd parts
                      [--no-warm]       database and config, leave the index cold
KBL_CLANGD=0 / KBL_CLANGD_WARM=0        the same two, from the environment
KBL_CLANGD_JOBS=N                       cap the parallel work of both steps

python3 lib/index/clangd.py --tree DIR warm [-j N] [--timeout S]   index on its own
```

`lib/index/clangd.py` can also report what an editor would see: `check` runs a real
clangd session (one `didOpen` per file, counting diagnostics) and `regions`
verifies clangd's inactive `#if` shading against the build.

```sh
python3 lib/index/clangd.py --tree ~/.config/kbuildlab/trees/2349b212ea4a9896 check --all -j 3 --journal /tmp/scan --resume
python3 lib/index/clangd.py --tree ~/.config/kbuildlab/trees/2349b212ea4a9896 regions --all -S 60
```

Both steps are incremental, keyed on the command, the code, clangd, the config and
every file kbuild recorded as read, so a rebuild repeats only what the change
touches; `regions --fresh` asks clangd again regardless.

## run and attach

Every command takes `-h`/`--help`.

```
kbuildlab run [tree] [options] [-- QEMU ARGS]
  --run                      boot now instead of freezing at the reset vector
  --boot direct|uboot|uefi   boot path (default: the tree's BOOT, else direct)
  --kaslr | --no-kaslr       randomized vs deterministic base (default: the tree's KASLR, else off)
  --kvm | --no-kvm           x86 only; TCG by default, for deterministic early-boot HW breakpoints
  --initrd PATH | --no-initrd
  --net | --no-net           user NIC with an ssh forward (default on); --ssh-port N
                             (default: the tree's SSH_PORT, else 2222)
  --persist | --no-persist   attach the tree's writable /persist disk
  --emulate | --no-emulate   attach emulated controllers for the soft presets' drivers
                             (NVMe, AHCI, virtio-scsi, USB, input, GPU, sound, CAN,
                             e1000e, ...; default: the tree's EMULATE)
  --modules | --no-modules   append the modules `kbuildlab build` staged to the initramfs
                             (default on whenever the build has modules)
  --append WORDS             extra kernel command-line words, repeatable
  --qmp-port N | --no-qmp    drgn's channel (default: the tree's QMP_PORT);
                             --no-qmp drops it and the vmcoreinfo device
  --port|-g N                gdb port (default: the tree's GDB_PORT)
  --mem|-m SIZE, --smp N     default: the tree's MEM and SMP, else 1G and 2; --bios PATH

kbuildlab attach [tree] [options] [-- GDB ARGS]
  --no-bootbreak|--raw       attach to an already-booted guest, no run-to-_text
  --stop text|firmware|start_kernel   where to stop (default text = head.S _text)
  --port|-p N                attach by port; the tree comes from the guest's run state
  --list|-l                  list live guests and exit; never prompts
  --first                    with several matches take the lowest port (for scripts)
  --json                     the session as JSON instead of starting gdb (with --list:
                             the live guests); the nvim adapter starts gdb from it
```

The console is the guest's serial line: **Ctrl-C reaches the guest** (it stops a
`ping` there), **Ctrl-A X quits qemu**, Ctrl-A C switches to qemu's monitor and back.
When `run` starts on a terminal, the guest console takes that terminal's rows and
columns, and follows it when the window is resized: `lib/winsize.py` answers on a
virtio-serial port named `org.kbuildlab.winsize`, and the rootfs's `S12winsize`
applies each size to the console tty with `stty`, which sends the shell and any
full-screen program `SIGWINCH`. A guest without that service, or a run whose
input is not a terminal, keeps the kernel's default size.

`attach` states the session to gdbtools rather than relying on its defaults, which
are all off: the memory guard, pwndbg's guards, the `kgdb`/`flow`/`msysreg` panels,
the quiet page scan, the arm64 disassembly guard, the shadow symbols, the KASLR
slide applied at the first virtual stop, the crossing catcher and breakpoint
siblings, plus `GDBTOOLS_BOOT`, from which gdbtools derives the hw entry break of a
firmware boot and x86's direct-boot decompressor address (both also stated, for an
older gdbtools). The environment and `tree.conf` win over each, an empty value
too (`GDBTOOLS_PANELS=` turns the panels off). It also passes
`add-auto-load-safe-path <source>`, so the kernel's `lx-*` commands load for that
session without any line in an init file.

The gdb-port flag differs by command: `run` names it `-g` (QEMU's spelling),
`attach` names it `-p`.

The rootfs carries none of the build's modules, so `kbuildlab build` stages them
in the tree's data directory as `modules.cpio.gz` (stripped), and `run` appends it to the
initramfs in every boot mode. An init script in the archive loads what the devices
present at boot ask for, as udev would, so an emulated controller whose driver is a
module binds. An archive older than the build's modules is left out, and `run` says
so.

`--boot uboot` and `--boot uefi` need a pre-built disk image (`BOOTDISK`, default
`boot/uboot.img` or `boot/esp.img` in the data directory); `--persist` needs an
`S45persist` init script
in the guest rootfs. The scripts that build these (`firmware/build-bootdisk.sh`,
`firmware/build-esp.sh`, `rootfs/post-build.sh`) belong to the workspace, not
kbuildlab; they take a tree ID and ask `kbuildlab inspect` where things are. The
records under `lab/workspace/` keep a copy, and `init --restore` puts them back. Without them a firmware boot stops with a message naming the disk.

`attach` runs gdb with its working directory in the kernel source, because the
kernel's own `scripts/gdb` commands resolve `vmlinux` and module paths relative to
it. A relative path you pass after `--` in a gdb path position (`-x`, `-s`, `-c`,
`-d`, `-e`, the first positional) is made absolute against your original directory
first, so the chdir does not re-root it.

### Several guests of one tree

`run` advances to a free port rather than refusing, so a second guest of the same
tree is normal — and its port appears nowhere in `tree.conf`. `attach` therefore
discovers rather than assumes:

- one live guest of that tree → attaches with no prompt;
- several → a chooser (fzf when present, else a numbered prompt), or a refusal
  naming the ports when stdin is not a terminal;
- `--port N` alone → the tree is recovered from that guest's own run state;
- a tree and `--port` that disagree → refused, saying what is on that port.

So `GDB_PORT` is `run`'s default, not `attach`'s. An explicit `--port` is refused
if the port is taken; a busy tree default auto-advances.

`attach --list` prints a table of live guests, including whether each already has
a debugger — read from `/proc`, never by connecting, since a bare TCP connect
pauses a running guest and leaves it paused. A second `attach` on an in-use port
does not usefully queue (the stub serves one client); `attach` says so rather than
appearing to hang.

```
$ kbuildlab attach v4.6-arm64 --list
live guests of v4.6-arm64:
  #   port   ssh    tree             boot    kaslr  started  now        dbg                  pid      uptime
  1   1435   2222   v4.6-arm64       uboot   off    frozen   prelaunch  -                    86359    00:41
  2   14991  2223   v4.6-arm64       uboot   off    running  running    attached             89478    00:12
```

### drgn

`run` opens a QMP monitor beside the gdbstub (a unix socket in the state directory
and a TCP port the tree states as `QMP_PORT`) and adds `-device vmcoreinfo`.
`kbuildlab drgn` finds the guest as `attach` does and hands drgn the channel and
the tree's `vmlinux`. Unlike the gdbstub, QMP neither pauses the guest
nor displaces an attached gdb, so drgn can read a running, gdb-attached guest.

Two halves have to line up, and `drgn` names whichever is missing:

- QEMU's `-device vmcoreinfo` advertises where the guest's VMCOREINFO note is.
- The guest writes that note, which needs `CONFIG_FW_CFG_SYSFS` and
  `CONFIG_VMCORE_INFO` (`CONFIG_CRASH_CORE` before 6.10) — both in the preset,
  and both needing a 4.17 kernel or newer.

The note is written from `qemu_fw_cfg`'s device initcall, after `start_kernel`
returns, so a guest frozen at reset or stopped at `start_kernel` has none — and
drgn builds its `Program` at connect, so a session opened too early never
recovers. `drgn` probes for the note before handing over, and `--wait` polls and
attaches the moment it appears.

drgn identifies the guest by itself only over the unix socket. `--tcp` reaches the
TCP port from elsewhere, but the note must then be supplied with
`-- --vmcoreinfo PATH`. A second `kbuildlab drgn` waits in the listen backlog until
the first quits (the monitor serves one client).

`scripts/drgn/ops.py` answers which implementation fills each slot of a handler
table, read from the guest's memory plus the tree's DWARF:

```
kbuildlab drgn 3bef -- scripts/drgn/ops.py shmem_ops
  alloc_inode          shmem_alloc_inode     mm/shmem.c:5113:1
  drop_inode           inode_just_drop       fs/inode.c:1956:1
  statfs               shmem_statfs          mm/shmem.c:3794:1
  sync_fs              NULL
```

`--grep RE` finds the tables, `--limit N` resolves the first few. A NULL slot is
printed as NULL: in a kernel that is a decision, not an absence.

**riscv64 is drgn's exception, not this tool's.** As of drgn 0.2.0 the guest is
identified and physical reads work, but every kernel virtual address raises
`FaultError: could not find memory segment` (under Sv39 and Sv48/Sv57 alike, so not
a paging-mode question). `drgn` says so before handing over; x86_64 and arm64 read
symbols normally.

### gdb and drgn together

They need no channel between them: gdb drives the guest through the gdbstub, drgn
reads it through QMP, and both see one machine.

| | QMP runstate | what drgn reads |
| --- | --- | --- |
| drgn alone | `running` | jiffies advance — QMP reads do not stop the guest |
| gdb attached | `paused` | identical across reads: the state gdb stopped, still `paused` afterwards |
| gdb SIGKILLed | `paused`, stub free within ~2s | unchanged; a fresh gdb attaches after `resume` |
| drgn SIGKILLed | unchanged | the monitor is released; the next session connects |
| guest killed | unreachable | run state and socket reclaimed by the launcher |

The one case that does not self-heal is the middle: a debugger killed while it
holds the guest releases the gdbstub at once but leaves the guest **paused**, and
nothing on the gdbstub side restarts it. `kbuildlab resume [tree]` is the way back,
over QMP; it refuses while a debugger is attached (`--force` overrides) and takes
the same `--port`/`--list`/`--first` discovery as `attach`. In `attach --list` a
row that is `paused` with no `dbg` is this stranded case (`started` is qemu's `-S`
flag, `now` is the live runstate).

### The run state

`run` records what it started in `$KBL_STATE_DIR/kbl-run-<port>.env` (default
`/dev/shm`), written whole and renamed into place. `attach`, `drgn` and `resume`
read it (the editor adapter reads it through `attach --json`); its `KBL_*` keys are
added, never renamed. It carries the boot mode, the firmware load address, the
KASLR state, the ssh port, the effective kernel command line (which a firmware
chain passes itself, so it is nowhere on qemu's command line), the QMP port and
socket, and qemu's pid with its `/proc` start time — which tells a file that
outlived its guest from the process now on that port.

### When a guest dies

A guest that is *ended* (Ctrl-A X, a closed terminal, kbuildlab tidying up) exits
quietly. A guest that *dies* is reported in this tool's own voice:

```
kbuildlab: guest on :1435 died of SIGSEGV -- it did not shut down.
```

A guest dies rather than exits most often when a debugger read of an address QEMU
dispatches into a device model SIGSEGVs it, which gdbtools' `kearly safemem` guard
exists to prevent. The launcher reports the signal in its own voice (bash's
job-control line names only this script's internals) and restores the terminal on
every exit path, since `-nographic`'s raw mode is not put back on a signal death.

## The tree.conf

`init` and `add` write it from `templates/`. It states what the tree is (`ARCH`,
`VERSION`, `UPSTREAM`, and `SRC`, the absolute path of the kernel source)
and the facts the commands consume: `QEMU_BIN`, `MACHINE`, `CPU`, `CONSOLE`,
`KERNEL_IMAGE_REL`, `GDB_PORT`, `QMP_PORT`, and where they apply `BOOT`, `INITRD`,
`PERSIST`, `EMULATE`, `CPU_PAGING`, the firmware paths (`UBOOT`, `BOOTDISK`,
`OVMF_CODE`/`OVMF_VARS`, `UEFI_ENTRY`) and the run defaults `MEM`, `SMP`, `KASLR`,
`SSH_PORT`. `GDB` names the gdb to use instead of the first that works;
`CMDLINE_EXTRA` adds kernel command-line words in every boot mode; `GDB_BIND` is
the gdbstub's address, default `127.0.0.1`, because a gdbstub is an
unauthenticated read/write channel into guest memory.

A path is absolute, `~/`-relative, or relative to the tree's data directory.
`CPU_PAGING` sets paging as CPU features without a rebuild (`"la57=off"` pins x86
to 4-level, `"sv48=off,sv57=off"` pins riscv to Sv39). `MEM` and `SMP` go to
qemu's `-m` and `-smp` as written. A u-boot boot loads the image where
`UBOOT_LOADADDR`/`UBOOT_RDADDR` say; only QEMU's `virt` machine has defaults
(arm64 at 1 GiB, riscv64 above u-boot at 2 GiB), and there an address outside the
guest's RAM is refused. Another machine states both, and `RAM_BASE` (hex) for the
same check. A UEFI boot needs `UEFI_ENTRY`, the image base the EFI stub chooses;
it is measured, not assumed.

**Grammar.** `KEY`, optional blanks, `=`, optional blanks, the value; the first
line for a key wins; a value that opens with `"` runs to the next `"`, any other
ends at the first `#` with trailing blanks dropped. `kbl_tree_get` in
`lib/common.sh` is the reference, `lib/treeinfo.py` and `lib/index/clangd.py` read it
the same way, and `tests/tree-conf.py` checks them against `tests/tree-conf-vectors.json`.

Every `GDBTOOLS_*` line is handed to gdb verbatim and wins over what `attach`
states, except `GDBTOOLS_ENTRY_PA`, which describes the tree's DEFAULT boot mode:
it applies only when that is what the port runs, and x86 KASLR takes it back.

Per-machine settings stay out of the repo: `~/.config/kbuildlab/workspace` (from
`init --workspace`, overridden by `$KBL_WORKSPACE`) and `KBL_*` variables
(`KBL_CONTAINER`, `KBL_IMAGE`, `KBL_JOBS`, `KBL_GDB`, ...) in a file your shell
sources, such as `~/.config/kbuildlab/env`. That file is for settings, not
secrets: the shell exports them to every program it starts. `sync` clones with `--depth 1` unless `KBL_FULL_HISTORY=1`, borrowing from
`KBL_MIRROR` when one is named.

## The debuggability preset

`presets/` holds a kernel configuration for one purpose: seeing as much as the
kernel can be made to show. It is split three ways because a naive
shared/per-arch split fails on a specific class of option.

| file | what it holds |
| --- | --- |
| `common.config` | what all three architectures can set identically |
| `arch/<arch>.config` | what looks shared but is not, plus each architecture's own |
| `soft/*.config`, `arch/<arch>.soft.config` | simulated devices, emulated controllers' drivers, and what traces them -- applied softly |
| `verify.sh` | reads the built `.config` back and fails on anything that did not survive |

When a tree tracks `UPSTREAM=mainline`, `arch/<arch>-mainline.config` stands in
for `arch/<arch>.config` where mainline has moved past the pinned release
(riscv64 ships one).

The soft half is applied after the rest by `lib/softconfig.py` and never moves an
answer the `.config` already has: a line that would build in a module subsystem is
taken as a module or left off, and a line for a symbol a kernel lacks or has
promptless is skipped. So one list serves every kernel version and arch defconfig,
and what each tree got is printed (on, lowered, as a module, or off for a missing
dependency). A kernel older than the one `common.config` was verified against takes
the hard half softly too, which is how v4.6 shares the presets.

Every line was checked against the 6.12 source. Promptless symbols are
deliberately absent: `make olddefconfig` recomputes them from their selecters, so
a hand-written line for one is discarded. `verify.sh` exists because a fragment
whose `depends on` is unmet is dropped silently, the build succeeds, and the
feature is missing at runtime; `kbuildlab config --preset` runs the check and
fails loudly instead.

### What it buys

- **Debug information kept whole.** DWARF5, not reduced, split or compressed.
  `CONFIG_DEBUG_INFO_REDUCED` adds `-fno-var-tracking`, the direct cause of
  `<optimized out>` on locals that were otherwise recoverable.
- **Symbols kept whole.** `KALLSYMS_ALL`, no `STRIP_ASM_SYMS`, no
  `TRIM_UNUSED_KSYMS`, a linker map, and BTF for drgn, bpftrace and CO-RE.
- **Tracing.** ftrace and function_graph with return values, the latency tracers,
  histogram and synthetic events, kprobes and uprobes with BTF-named arguments,
  blktrace, perf — enough for trace-cmd, kernelshark, bpftrace, uftrace and
  `perf trace`.
- **eBPF and XDP.** Syscall, JIT, LSM, BTF, and the attach points the tooling
  expects.
- **The early memory map.** `PTDUMP_DEBUGFS` exports every kernel mapping with its
  permissions — `/sys/kernel/debug/kernel_page_tables` on arm64 and riscv, a
  `page_tables/` directory (`kernel`, `current_kernel`, `current_user`, `efi`) on
  x86. `DEBUG_WX` warns on a W+X mapping, `DEBUG_VIRTUAL` catches `__pa()` on an
  address outside the linear map, `MEMTEST` reports bad ranges. `ARCH_KEEP_MEMBLOCK`
  is deliberately *not* set (promptless; on x86_64 only `INTEL_TDX_HOST` selects
  it), but the arrays are live before `mem_init()` and gdbtools' `kmemblock` reads
  them there. memblock's own commentary is boot-parameter, not config
  (`memblock=debug`, `early_ioremap_debug`, `mminit_loglevel=4` via `CMDLINE_EXTRA`).
  v4.6 restricts `DEBUG_WX`/`DEBUG_VIRTUAL` to x86; its `ARM64_PTDUMP` equivalent is
  in `arch/arm64.soft.config`.
- **Devices without the hardware.** `soft/simulators.config` turns on what the
  kernel can stand in for (`IRQ_SIM`, IIO, null_blk, scsi_debug, netdevsim,
  wireless/Bluetooth/NFC, V4L2/CEC/DVB test devices, virtual KMS, dummy sound and
  USB, uinput/uhid, a virtual TPM, ...); `soft/emulated.config` builds the drivers
  for what `run --emulate` emulates, so real drivers probe real controller models.
- **Seeing them work.** `soft/observability.config` adds `DYNAMIC_DEBUG`,
  `GENERIC_IRQ_DEBUGFS`, `SCHEDSTATS` (for `perf sched`), usbmon, the drop monitor,
  pktgen, mac80211 trace events, and the debugfs views of block, NVMe, SCSI, V4L2
  and ALSA.

### What it does not

6.12 offers only `-O2` or `-Os`, no `-O0`/`-Og`. At `-O2`, even with everything
above:

- a local whose lifetime has ended has had its register reused, so gdb saying
  `<optimized out>` there is accurate rather than a defect;
- `static inline` in headers is gone from `nm`, kallsyms, ftrace and BTF,
  surviving only as `DW_TAG_inlined_subroutine` in DWARF;
- `.isra.N` clones survive — no flag the kernel offers removes them;
- hand-written assembly entry paths keep no frame-pointer discipline, so a
  backtrace ends at the kernel entry boundary.

Self-tests and profilers are off on purpose (`FTRACE_STARTUP_TEST` and
`RING_BUFFER_STARTUP_TEST` would run on every boot): the capability is present and
idle, not every diagnostic armed.

### Architecture differences that are not preferences

- **riscv64 has no *in-guest* hardware breakpoints** (`HAVE_HW_BREAKPOINT` is
  absent), so in-guest `perf record -e mem:...` and ptrace watchpoints are
  refused. Debugging over the QEMU gdbstub is unaffected: QEMU enforces `watch`
  (and gdbtools' `kw`) in the emulator.
- **riscv64 ftrace and preemption are mutually exclusive** — `HAVE_FUNCTION_TRACER`
  is selected `if !XIP_KERNEL && !PREEMPTION`, and Kconfig does not warn, it simply
  stops offering it.
- **arm64 and riscv64 have no `DYNAMIC_FTRACE_WITH_REGS`**, so `FPROBE`,
  `KPROBES_ON_FTRACE` and BPF `kprobe_multi` are unreachable on both.
- **arm64 loses BPF trampolines under CFI** — `DYNAMIC_FTRACE_WITH_DIRECT_CALLS`
  sits behind `DYNAMIC_FTRACE_WITH_CALL_OPS` behind `!CFI_CLANG`.

Each arch fragment states these where you will read them.

## Containers

`containers/Containerfile` is plain OCI with no builder extensions, so **docker,
podman, buildah and nerdctl all read it unchanged**; `Dockerfile` beside it is a
symlink for tools that look for that name first. `kbuildlab` picks a runtime in
the order podman, docker, nerdctl (buildah reads the `Containerfile` but cannot
run a guest, so it is not auto-selected), or takes `$KBL_CONTAINER`, which wins.
podman comes first because rootless podman needs no daemon.

`kbuildlab image` builds the default Ubuntu 26.04 image. `kbuildlab image legacy`
builds an Ubuntu 24.04 image (`containers/Containerfile.legacy`, gcc-9 through
gcc-13, aarch64 cross only) — **no tree here uses it**, but it is the answer for
building a 2016 kernel with an era-appropriate compiler. `containers/Containerfile.legacy-gcc6`
(Debian 9, gcc-6.3) goes back further; `kbuildlab image` does not build it, and it
has no python3, so a tree on it needs `KBL_CLANGD=0 KBL_TAGS_VIEW=0` for `tags`.

Each image carries several compiler generations: 14, 15 and 16 in the default
image (with aarch64 and riscv64 cross compilers), 9 through 13 in the legacy one
(aarch64 only). A tree names the one it builds with, in its own `tree.conf`:

```
IMAGE=kbuildlab-legacy:latest   # which image; unset means the default one
GCC=16                          # which compiler in it; unset means the image's own gcc
HOSTCC_FLAGS=-fcommon           # appended to HOSTCC only; usually unset
KCFLAGS=-fvar-tracking-assignments   # appended after the kernel's own CFLAGS
```

"The newest toolchain" is not one answer across these trees: gcc-10 turned
`-fno-common` on by default and a pre-5.6 kernel's host tools then collide at link;
mainline tracks compiler trunk, a stable series was validated against what shipped
with it. So the compiler belongs to the tree, is found by building, and `kbuildlab
build` reports which it used. The unversioned `gcc` in the default image is **15**;
**16** is installed but is a dated development snapshot (`16-2026MMDD`), so opting
into it is deliberate.

- `HOSTCC_FLAGS` is appended to `HOSTCC` (not set as `HOSTCFLAGS`, which would
  replace the kernel's own flags) and reaches host tools only. That is how **v4.6
  builds on gcc-16 with `HOSTCC_FLAGS=-fcommon`**, with the kernel's own code
  generation identical to its gcc-9 unaided build.
- `KCFLAGS` is appended after everything the kernel's Makefile set, so it wins —
  the way to undo a flag the kernel hard-codes without editing it; keep it to
  debug-information flags. kbuildlab also adds a few off the Makefile itself:
  `-fvar-tracking-assignments` to a kernel that turns it off (v4.6, so optimised
  locals stay printable), `-fno-PIE` to one that never turns PIE off (whose arm64
  modules otherwise fail to build), and the `make-cmd` `$(pound)` fix so a
  make-4.2-era kernel builds incrementally under make 4.3+.

| tree | builds with | note |
| --- | --- | --- |
| `mainline-*`, `v6.18-*` | gcc-16 | nothing found |
| `v4.6-arm64` | gcc-16 + `HOSTCC_FLAGS=-fcommon` | gcc-9 unaided; host tools only |

Only the compiler is versioned: `CROSS_COMPILE` still supplies `ld`, `as` and
`objcopy` (one version per target), and `HOSTCC` moves with `CC` so the host tools
that parse the kernel's output are not a generation apart from it.

`kbuildlab containers` (`containers status`) reports what the runtime holds and
the four ways an Ubuntu container can collide with the host, each checked rather
than assumed:

- **The environment.** The image is asked what it actually receives, and any
  build-steering variable seen inside (`CC`, `CFLAGS`, `MAKEFLAGS`,
  `CROSS_COMPILE`, ...) means the runtime is leaking the host environment.
  Expected answer: *clean*.
- **The toolchain.** Each tree's recorded `CONFIG_CC_VERSION_TEXT` is compared
  against the compiler that would build it now — per tree, since a tree names its
  own image and version. A mismatch is `MIXED` (wants `make mrproper`); a missing
  compiler is `NO CC`, a missing image `MISSING`, and a tree older than 4.19 (no
  `CONFIG_CC_VERSION_TEXT`) `unchecked`. The `Containerfile`'s own `gcc` default is
  checked too and reported `STALE` if it was edited without a rebuild.
- **The mount.** A build sees the tree's source and data directory read-write and
  its `tree.conf` directory and this repository read-only, each at the path it
  resolves to and at the spelling it was given; a read-write one inside `/usr`,
  `/etc`, `/var` or another system directory is refused. Each tree's source and
  data directory are put through that check here, and searched for root-owned
  files (the mark of a rootful run).
- **The host's own config.** `/etc/containers` is checked for unmerged `.pacnew`
  files, rootless podman for its `subuid`/`subgid` ranges.

`kbuildlab containers clean` removes the stopped containers made from kbuildlab's
images and nothing else; `--images` also drops the images kbuildlab built;
`--dangling` additionally removes every untagged image on the host (not only
kbuildlab's). Nothing here prunes the runtime wholesale.

## Layout

```
bin/kbuildlab          globals, usage and dispatch
lib/common.sh          shared helpers: paths, tree resolution, run state, output
lib/registry.sh        trees, inspect, add, get/set/unset, edit, mv, rm, prune
                       (lib/treeinfo.py reads what they report)
lib/build.sh           the build container and its toolchain; sync, config, build,
                       module staging (lib/softconfig.py applies the soft preset)
lib/lab.sh             init and the lab/ records
lib/tags.sh            tags: ctags, cscope, gtags and the clangd index
lib/update.sh          update
lib/host.sh            doctor, image, containers
lib/run-qemu.sh        run; run-gdb.sh, run-drgn.sh and resume.sh are attach, drgn
                       and resume, each a program of its own (lib/qmp.py talks QMP)
lib/cli.py             every command's options, for --help and Tab completion
lib/index/             the indexers: clangd.py, the tag view, the build readers
completions/           the bash and zsh completions install.sh puts in place
tests/                 tree.conf grammar and cli.py against the parsers
```

## Dependencies

Required (see Install for versions): a container runtime, `qemu-system-<arch>`,
and a gdb that can debug the target -- a cross gdb, `gdb-multiarch`, or a host gdb
built `--enable-targets=all`. A candidate whose own Python layer does not start is
skipped with gdb's reason; `GDB` in `tree.conf` or `$KBL_GDB` names one outright.

Optional: [gdbtools](https://github.com/yugeun-song/gdbtools), which makes symbols
resolve before the MMU is on; `kbuildlab attach` finds it if it is installed and
says so if it is not. `clangd` and `python3` on the host, for the clangd index;
absent, `kbuildlab tags` says so and builds the other three.

Nothing is installed for you, and no system setting is changed.
