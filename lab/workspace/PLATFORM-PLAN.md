# Platform realism rework (started 2026-10-19) -- context to resume from

Read this first when resuming. The user may be cut off by the weekly plan limit;
everything needed to continue is here or in `logs/platform-2026-10-19/`.

## The request (user, 2026-10-19, condensed; all of it still applies)

1. Generation work goes to the Console-credit worker (`~/workspace/credit-worker/credit-run`,
   Opus, effort xhigh), used actively. This session reviews, applies, builds, tests.
2. Each of the 7 kernels runs in an environment as close as possible to the devices
   that typically run its architecture, with the most common yet modern options of
   2026. Find every outdated option (pc/i440FX, DVD-ROM, ...) and replace or drop it.
3. x86_64 = desktop/server: UEFI (OVMF -> GRUB2, already so). arm64/riscv64 = embedded:
   U-Boot + DT, no UEFI.
4. Storage: x86 NVMe; arm64/riscv64 the typical embedded storage (eMMC chosen).
5. Root fs: x86 ext4; arm64/riscv64 "the most common option" = ext4. FAT boot partition
   (most standard on embedded), GPT.
6. Ask when needed.
7. Virtual Ethernet, XDP, RDMA where the kernels support it. InfiniBand link layer and
   EtherCAT are given up (no QEMU model / out-of-tree only). v4.6 lacks XDP/rxe/siw/gpio-sim:
   accepted, do not work around.
8. Embedded buses (I2C, SPI, UART, GPIO, CAN) where possible WITHOUT touching kernel
   source: the kernel trees stay pristine (only the user edits lines by hand for tests);
   no third-party kernel patches.

Mid-course rulings:
- Integrate into kbuildlab generically; hand-edit only what cannot be generalized.
- Do not wipe existing kernel options; change only what this needs (done: see below).
- PCIe and Ethernet are mandatory on arm64/riscv64; virt/virtio beats real boards in a
  trade-off. Boards are optional extras, lowest priority.
- riscv64 = SiFive stack: virt + `sifive-u54` CPU + PLIC + OpenSBI -> U-Boot. `sifive_u`
  (no PCIe) only as an optional board; arm64 optional board = `imx8mp-evk`.
- Use all cores, finish tonight (overrides the one-core rule for this task); keep the
  memory/tmpfs/zram watchdog.
- Test kbuildlab/gdbtools after the change, fix bugs, beware false positives.
- RISC-V is 64-bit only; never configure a 32-bit guest.
- When everything is done: verify that the existing debugging and custom features
  (kbuildlab run/attach/drgn/resume, gdbtools early-boot walk, KASLR, persist, winsize,
  modules, emulate) work with the new kernels and platforms, fix bugs, then commit AND
  push every repository changed (kbuildlab, gdbtools if touched), following each repo's
  own message convention, no AI trailers (CLAUDE.md §6; check `git log -1 --format=%B`).

More rulings (21:40-22:15):
- Every device and driver is the newest QEMU supports; old emulated parts (igb, e1000e
  style) lose to virt/virtio. Already-applied items that fail this were reverted.
- arm64 CPU cortex-a710 (Armv9) + GICv4 (verified: v6.18 and v4.6 boot).
- UFS 4.0 was chosen for arm64/riscv64, then given up: U-Boot v2026.07's ufs-pci driver
  cannot talk to QEMU's UFSHCI 4.1 model ("invalid UFS version 0x410", OCS 1 on every
  query); only source changes or hacks (split boot device, kernel over fw_cfg) would
  help, which the user ruled out. Back to eMMC; all UFS bits removed again (kernel
  configs restored exactly to the pre-UFS state, presets/run-qemu/U-Boot fragments).
- Never modify kernel or U-Boot sources; when that or a hack is the only way, give up.
- Generic options go into kbuildlab presets/templates; per-tree specifics stay local.
  Later (not now): a fallback/stop feature for symbols a kernel version lacks or renamed.
- Final verification must boot all 7 and drive each from head.S under gdbtools to past
  login, including breakpoints and watchpoints; add kbuildlab/gdbtools features where
  needed; then commit + push.

## Target per architecture

| | x86_64 (0c5b, afce) | arm64 (2349, 3bef, 2fc5=v4.6) | riscv64 (805a, 3367) |
|---|---|---|---|
| machine | `q35,sata=off,i8042=off,vmport=off` (was pc) | `virt,gic-version=4,virtualization=on` | `virt` (PLIC) |
| cpu | max (TCG) / host, la57=off | cortex-a710 (was a72), GICv4 | sifive-u54 (was rv64); Sv39 only |
| firmware | OVMF -> GRUB2 | U-Boot (EL2) | OpenSBI -> U-Boot (was direct); kernel at 0x84000000 |
| defaults | `-nodefaults` (no DVD/floppy/LPT/VGA) | same | same |
| system disk | NVMe ns1: GPT ESP(FAT32)+ext4 | eMMC (sdhci-pci): GPT boot(FAT)+ext4 | same as arm64 |
| /persist | NVMe ns2 | SD card (2nd sdhci-pci) | same |
| NIC | virtio-net-pci (+ 2 virtio LAN NICs) | same | virtio-net-pci (was mmio) + 2 LAN |
| USB | qemu-xhci | qemu-xhci | qemu-xhci |

Verified by probes (scratch, 2026-10-19): q35 -nodefaults OVMF boots GRUB from an NVMe
namespace; arm64 GICv3+ITS, EL2+VHE, cortex-a55 boots v6.18 and v4.6; riscv virt PCIe
works with PLIC and AIA; v6.18-arm64 boots on imx8mp-evk (I2C/MMC/GPIO/FEC/PCIe up);
v6.18-riscv64 boots on sifive_u (SPI NOR, SD-over-SPI, GEM). gdbtools already selects
SCTLR/TTBR by CurrentEL (EL2/VHE), still to be proven by the debug suites.

QEMU syntax settled: eMMC/SD behind named root ports, no root-bus name needed:
`-device pcie-root-port,id=kblrp0,chassis=N -device sdhci-pci,id=kblmmc0,bus=kblrp0
 -device emmc,drive=D,bus=kblrp0/kblmmc0/sd-bus` (sd-card likewise). eMMC accepts a
non-power-of-two image. NVMe: `-device nvme,id=kblnvme,serial=kblnvme -device
nvme-ns,drive=D,bus=kblnvme,nsid=N`. U-Boot bootmeth_script exports devtype, devnum,
distro_bootpart to boot.scr.

## Kernel config (DONE 2026-10-19, before builds)

One-time migration of the existing .configs, nothing else moved:
- hard: `kbuildlab/presets/storage/{nvme,emmc}.config` (x86 nvme, others emmc) and
  `CONFIG_SCSI_DEBUG=m` (built in it attached a fake SCSI disk at boot).
- soft (only new lines, lib/softconfig.py): igb/igbvf, sdhci-pci, usb-serial ftdi,
  `presets/soft/network.config` (XDP sockets, cls/act bpf, clsact, veth, IB core,
  rxe, siw), `presets/soft/embedded.config` (i2c-dev, spidev, spi/i2c-gpio, gpio cdev,
  CAN protocols, lm75/tmp421/emc1403/at24/ds1307/pca953x/dps310/spi-nor).
- presets/soft/simulators.config now says SCSI_DEBUG=m for new trees.
- Diff check: the only existing answers that moved are SCSI_DEBUG y->m and, where only
  scsi_debug selected it, CRC_T10DIF y->m. Before/after copies:
  `logs/platform-2026-10-19/configs/<id>.{before,after}` (rollback = copy .before back).
- Helper scripts: `logs/platform-2026-10-19/bin/` (apply-config.sh, probe.sh,
  build-all.sh, watchdog.sh).

## Work items and status

- [x] decisions, probes, config migration
- [x] arm64 EL2/VHE + GICv3 + cortex-a55: full-verify 8/8 on 2fc5 and 3bef (old kernels);
      gdbtools kpt walks TTBR1_EL2 to swapper_pg_dir (real values checked)
- [x] riscv64 sifive-u54 + OpenSBI -> U-Boot: full-verify 8/8 on 3367 (old kernel).
      Causes found and fixed: (1) U-Boot's riscv booti always moves the Image to RAM base
      + text_offset (0x80200000), so UBOOT_LOADADDR=0x80200000 and GDBTOOLS_ENTRY_PA=
      0x80200000; QEMU also places U-Boot there, so (2) gdbtools now skips a stop at the
      armed entry PA while the arch's image magic is absent (boot.py _firmware_at_entry,
      session.on_stop defers the re-arm with gdb.post_event: creating a breakpoint inside
      a stop event crashed gdb 18.1); (3) kbuildlab run-gdb.sh no longer loads u-boot
      symbols that overlap the kernel entry on a run-to-_text (gdb 18.1 aborted in
      breakpoint_free_objfile when removing them) and removes symbols by file name.
      TODO: kbuildlab run-qemu riscv virt default UBOOT_LOADADDR 0x84000000 -> 0x80200000
      (after W1's diff is applied); recheck `attach --stop firmware` on riscv.
- [x] kernel builds, all 7 (all built; later rebuilt after the UFS revert, 805a for NLS).
      Original note: `KBL_JOBS=8 kbuildlab build <id>`: 2349 done (1101 s). The
      watchdog stopped the rest at 21:16 (MemAvailable 1.1 GiB during the riscv vmlinux
      link while QEMU/gdb tests and two workers ran); restarted 21:30 for 805a 0c5b 3bef
      3367 afce 2fc5. Keep heavy tests off the machine during links. Rerun any missing
      tree the same way; the build is incremental.
- [x] kbuildlab run-qemu (W1, $8.07, applied with GNU patch fuzz; change lines verified
      identical to the worker diff): STORAGE, ROOT=disk, SYSDISK/BOOTPART_SIZE/ROOTFS_IMAGE/
      EFI_LOADER, fw_cfg modules, -nodefaults, NIC_DEVICE, LAN_NICS, QEMU_ARGS,
      --root/--reset-sysdisk; riscv virt default load address fixed to 0x80200000.
      Not yet boot-tested.
- [x] kbuildlab config: presets/storage/$STORAGE.config applied as a hard fragment
      (lib/build.sh; presets/storage/{virtio,nvme,emmc}.config). README row still to add.
- [x] workspace (W2, $6.80, applied; originals in logs/platform-2026-10-19/pre-W2/):
      defconfigs (+ext4 2560M, eudev, kmod, XDP/RDMA/embedded/introspection tools),
      post-build (S09kbl-modules fw_cfg installer, kbl-getty on the kernel console,
      eudev net-name rules masked), firmware/build-uboot.sh + u-boot-*.config fragments,
      firmware/build-grub-efi.sh (firmware/grub/BOOTX64.EFI built).
- [x] tree.conf x7 (logs/platform-2026-10-19/bin/apply-treeconf.sh); templates still to do
- NIC decision (user, 21:35): no igb -- QEMU has no Broadcom/Mellanox model and its Intel
  NICs are 2008 parts; every NIC is virtio-net (primary + LAN_NICS "virtio-net-pci
  virtio-net-pci"). igb removed from presets/soft/emulated.config and reverted in the
  four trees where this session had added it (805a 3367 0c5b afce); 805a/0c5b rebuild
  chained after the running batch.
- [x] U-Boot rebuilt for arm64 and riscv64 with MMC over PCI (fragment lines verified);
      previous binaries saved in logs/platform-2026-10-19/pre-W2/u-boot-*.bin.orig
- [x] buildroot rebuild x3, ext4 images (x86_64 706 s, arm64 549 s, riscv64 448 s)
- [x] x86_64 v6.18 disk boot verified by value: OVMF -> GRUB on nvme ns1, root nvme0n1p2
      ext4, fw_cfg modules installed, /persist nvme0n2, eth0 primary virtio + eth1/2 LAN,
      only the two generic SCSI lines left, all requested tools present, rxe0 on eth1
- [x] arm64 v6.18 U-Boot -> eMMC (sdhci-pci) boot verified (initramfs root); U-Boot needed
      mmc in boot_targets (firmware/u-boot-arm64.config)
- [x] kernels rebuilt after the UFS revert (22:15); x86 full-verify 8/8 on 0c5b and afce
- [x] e2e harness logs/e2e-verify.sh (head.S entry -> si -> start_kernel -> watch -> boot on
      disk -> ssh login -> break in the sync syscall with comm == "sync" -> watch -> detach ->
      root device/fs/modules/eth0 checked). Its first run reported a FALSE login PASS
      (askpass path missing, loop did not test success); fixed, x86 0c5b then 11/11 real.
- [x] fw_cfg module delivery abandoned: v6.18's fw_cfg sysfs reads byte-wise with a
      per-page re-skip (quadratic); a 29 MB arm64 archive never finished. Modules now go
      on the boot partition as /modules.cpio.gz (kbuildlab mcopy per run), S09 mounts
      partition 1 of the root disk read-only and installs on change. Storage presets now
      also force FAT/VFAT/NLS 437/iso8859-1 built in (only 805a moved: NLS_ISO8859_1 m->y).
- [x] rebuilt 805a (NLS), rootfs x3 (new S09); session-made system disks deleted
- [x] riscv U-Boot: distro boot's `mmc dev 0` found no device while the PCI SD host was
      unprobed; preboot now runs `mmc list` first (firmware/u-boot-riscv64.config)
- [x] e2e first pass (22:50): 0c5b, 2349, afce, 3bef 11/11; 805a stopped by me (U-Boot mmc
      probe, fixed above); 3367 showed start_kernel hit twice once the harness counted it
- [x] gdbtools fixes for riscv (3367 rechecked: one stop at start_kernel, 2 objfiles,
      watchpoint Old 0xfffedb08 -> New 0xfffedb09, shadow add/remove at the same text PA):
      (1) breakpoints.py: a location on a function's ELF symbol without line info is
      disabled when another location of the same breakpoint lies further into that
      function with line info (riscv patchable entry: minsym ...a80, DWARF ...a88);
      bpfix leaves it disabled. (2) boot.py _shadow_load caches the entry symbol's link
      VA (`_shadow_link`) while only vmlinux is loaded; symval() at a physical PC
      resolved in the shadow itself (text_pa 0x180400000) and shadows piled up.
- [x] e2e rerun 2 (23:17): 7/7 boot to login, 805a/3367 bSK flagged FAIL = harness regex
      (stop is start_kernel + 8 on riscv); the 805a log also showed `b ksys_sync` stopping
      at the bare ELF symbol whose line info is borrowed from the previous function
- [x] gdbtools split-entry rework (replaces the bare-entry rule): per function instance
      and regime only the location furthest into the function stays (`_entry_duplicates`,
      `_minsym_at`); re-applied at the next stop after any new objfile (shadow reload at
      the KASLR crossing re-created locations enabled; `_entries_stale`, `_fn_bps`);
      ADOPT no longer adds a sibling where the function already has a location in that
      regime (it added hbreak at schedule+0 -> two stops per call); `@safe()` restored on
      `_apply_bpfix` (an earlier edit had moved it). Verified live on 805a: setup_vm once,
      start_kernel once, schedule +24 once per call (3 calls, 3 sp values), ksys_sync +8.
      docs/early-boot.md note added. Harness bSK regexes (e2e, full-verify) accept + N.
- [x] e2e rerun 3 (23:40-23:45): 7/7 x 12/12 PASS with real values
      (logs/platform-2026-10-19/e2e-run3.result). NVMe namespace names may swap between
      boots (root nvme0n2p2 once); root is by PARTUUID, /persist by label, so harmless.
- [x] full-verify 7/7 x 8/8 = 56/56 PASS (23:45-23:48, logs/platform-2026-10-19/
      full-verify-final.result)
- [x] regress.sh run 1 (regress-run1.result): modules (scsi_debug modprobe from the
      boot-partition archive), perf, bpftool, gdb/gdbserver 15.2, strace, rxe + siw
      PORT_ACTIVE, XDP generic attach of a host-built xdp_pass.o, drgn (x86, arm64), and
      /persist across a restart PASS on 0c5b afce 3bef 3367 2fc5. v4.6: no rxe/siw/XDP,
      bpftool prog show unsupported (accepted). 2349 and 805a overlapped full-verify's
      guest of the same tree: the second run got a throwaway system-disk copy and no
      /persist, with the message -- the several-guests path works; rerun for /persist.
      Harness flaws: tcpdump waited for traffic nothing sent (now generates a TCP SYN);
      no virtio-ports because stdin was not a tty (winsize tested on a pty instead).
- [x] winsize on a pty (python pty, serial login): 0c5b, 2349, 805a get 50x200 at login
      and follow a resize to 40x150; /dev/virtio-ports/org.kbuildlab.winsize present
- [x] BUG fixed (kbuildlab run-qemu, W1 code): an interactive x86 ROOT=disk run hung
      before printing anything: `mmd ::/EFI` on the existing ESP directory asks on a
      terminal. Now `mmd -D s` (skip). Reproduced and verified on a pty.
- [x] U-Boot fragments moved to firmware/u-boot-{mmc-pci,arm64,riscv64}.config: kbuildlab's
      lab/ record keeps only top-level files of each workspace directory, so the
      u-boot-fragments/ subdirectory would have been lost on `init --restore`
      (build-uboot.sh updated; rebuild check pending)
- [x] regress rerun (00:16-00:21, regress.result): /persist across a restart PASS on 0c5b
      2349 805a 3367 too (all 7 now). drgn on riscv64: kbuildlab already says drgn 0.3.0
      translates no riscv64 kernel VA (known drgn limit); v4.6 publishes no vmcoreinfo.
      tcpdump: the guest has no `timeout` (busybox applet absent), which was the 0;
      without it a SYN to 10.0.2.2:1 is captured on 0c5b, 2349, 805a.
- [x] `attach --stop firmware` (fwstop.sh -> fwstop.result): arm64 reset 0 -> u-boot
      board_init_f (with symbols) -> kearly bootbreak -> _text -> start_kernel. riscv64
      ABORTED gdb 18.1 after `break start_kernel`: gdbtools removed the shadow with
      `remove-symbol-file -a 0x80200000`, which names u-boot's file first (linked at the
      kernel's load address). Fixed in gdbtools boot.py `_shadow_unload` (remove by file
      name); riscv64 now reset 0x1000 -> board_init_f -> entry -> start_kernel+8.
- [x] U-Boot rebuilt with the moved fragments: both .config files identical to before
- [x] boards.sh (W4, boards.result): imx8mp-evk on 2349 meets the W4 acceptance
      (ttymxc1, FEC eth0 DHCP + ssh, gpiodetect lists the pca6416 as gpiochip5 [2-0020],
      tmp105 binds, attach -> _text -> start_kernel). sifive_u on 805a: ttySIF0, GEM eth0,
      SPI NOR spi0.0 (32 MiB) in /proc/mtd; attach never reached the entry.
- [x] sifive_u attach: QEMU shows the E51 and the U54 cluster as two gdb inferiors and
      the kernel runs on inferior 2, which a plain `target remote` never attaches.
      Board key GDB_INFERIOR=2 -> run-gdb.sh connects extended-remote, add-inferior,
      attach 2, `file vmlinux` there only, schedule-multiple on. pwndbg (init file)
      makes gdb 18.1 hit internal-error inferior_thread on the switch to a thread-less
      inferior, so that session runs gdb -nx (said in one line); vmlinux in inferior 1
      too made a twin location at every address and the step-over re-hit it. Verified:
      _start head.S:32 on thread 2.1, si, start_kernel+8 once, bt, jiffies watchpoint.
      README Boards paragraph added.
- [x] run-tests.sh 37/37 PASS (00:49-00:50, run-tests-final.tsv); final e2e 7 x 12/12 PASS
      (00:51-00:56, e2e-final.result) with every code change in place
- [x] verification: all 7 boot by default; storage/rootfs/NIC/devices checked by value;
      full-verify 56/56, run-tests 37/37; harness false results found and fixed on the way
- [x] optional boards: see boards.sh above (both boot; attach works on both)
- [x] run-gdb.sh: u-boot symbols skipped on a run-to-entry when their LOAD segment covers
      the entry PA; removal by file name
- [x] cli.py: `_tree_dir` resolved ids wrongly (treeinfo.resolve returns an id), completion
      of code keys iterates treeinfo.ids()
- [x] regression of pre-existing features with the new kernels/platforms:
      - [x] logs/full-verify.sh on all 7 (56/56)
      - [x] logs/run-tests.sh (37/37)
      - [x] kbuildlab drgn on a booted guest (x86, arm64; riscv64 is a drgn limit)
      - [x] /persist on NVMe ns2 and the SD card, kept across a restart (all 7)
      - [x] modules: the boot-partition archive installs; scsi_debug modprobes (all 7);
            the initramfs path is used by --board runs (module coldplug at boot)
      - [x] winsize (pty, 3 arches), ssh forward, several guests of one tree
      - [x] guest tools: perf, bpftool, gdb/gdbserver, strace, tcpdump
- [x] REPORT.md section 14 (Korean); memory update
- [x] commit + push (01:10): kbuildlab 5d38c12..4912dd0 (8 commits), gdbtools
      3cc49ee..9b9de1c (4 commits), both on main, no trailers. Units:
      kbuildlab: cli completion by ID; config storage presets; soft presets; run GPT system
      disk/STORAGE/-nodefaults/LAN NICs; templates; attach u-boot symbols; run --board +
      GDB_INFERIOR; then `lab:` record (pending). gdbtools: loader at the entry PA; shadow
      link address; shadow removal by file name; split-entry breakpoints.
      Rule: one commit per coherent unit, repo convention, verify `git log -1 --format=%B`,
      then `git push` (kbuildlab and gdbtools, branch main).
