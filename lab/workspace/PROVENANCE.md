# Kernel source provenance

Every kernel source here came from kernel.org over HTTPS, and each tree's HEAD is
checked against the live `git ls-remote` answer of its origin after every update.

## Sources

- stable (v4.6.7, linux-6.18.y): `https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git`
- mainline (the mainline trees): `https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git`

These are kbuildlab's own defaults (`STABLE_URL`, `MAINLINE_URL` in `bin/kbuildlab`).

## Trees, verified 2026-10-15

| Tree | kbuildlab ID | Ref | Source | Commit |
|---|---|---|---|---|
| `v4.6-arm64` | `2fc57a9a83d77b6d` | tag `v4.6.7` | stable | `096998b11906dd79df5af4d688c9974342dd09f2` |
| `v6.18-x86_64` | `0c5b6d04f97ab622` | `linux-6.18.y` (v6.18.56) | stable | `17ad2612c11a748b50d32eefc681344b26fea946` |
| `v6.18-arm64` | `2349b212ea4a9896` | `linux-6.18.y` (v6.18.56) | stable | `17ad2612c11a748b50d32eefc681344b26fea946` |
| `v6.18-riscv64` | `805a3e5c570cd6cf` | `linux-6.18.y` (v6.18.56) | stable | `17ad2612c11a748b50d32eefc681344b26fea946` |
| `mainline-x86_64` | `afce473b9a94c424` | `master` (v7.3-rc6-386) | mainline | `a5ebb76233b79db01e82e061173cb73c6d2b5c6b` |
| `mainline-arm64` | `3bef7c6ecd23df86` | `master` (v7.3-rc6-386) | mainline | `a5ebb76233b79db01e82e061173cb73c6d2b5c6b` |
| `mainline-riscv64` | `3367ece228e67594` | `master` (v7.3-rc6-386) | mainline | `a5ebb76233b79db01e82e061173cb73c6d2b5c6b` |

On 2026-10-15 kernel.org answered `17ad2612c11a` for both `refs/heads/linux-6.18.y` and
`refs/tags/v6.18.56^{}`, and `096998b11906` for `refs/tags/v4.6.7^{}`, equal to the
local trees. The mainline trees were last compared on 2026-10-11, when `a5ebb76233b7`
was the live `master`; kernel.org's `master` has since moved (`0fcb05b72344` on
2026-10-15), and `kbuildlab update` follows it. v4.6.7 is the last 4.6.y release.

## Why the hashes are enough

- git addresses every object by its content, so equal commit hashes mean equal trees.
- The comparison is against the server's live answer, which anyone can repeat.
- Every fetch is HTTPS.

## Layout

The stable history was fetched once into `.mirrors/linux.git`, with mainline fetched
on top. Each tree's `kernel/` was cloned from that mirror, with full history, and its
`origin` is the kernel.org URL above. No tree shares a file with the mirror or with
another tree: the v6.18 trees were cloned with `--dissociate`, and the packs that
v4.6-arm64 and the mainline trees once shared through hardlinks were copied out on
2026-10-15, so a tree can be patched, repacked or deleted on its own.

## buildroot and u-boot

Pinned to release tags and not moved by kernel updates. The rootfs builds inside the
`kbuildlab:latest` container; early-boot debugging needs no rootfs.

| What | Where | Tag | Source | Commit |
|---|---|---|---|---|
| buildroot | `.buildroot/buildroot` | `2026.02.3` | `https://git.buildroot.net/buildroot` | `679b9ead7620bbf193620d1ebf56f53c1764d37a` |
| u-boot | `firmware/u-boot` | `v2026.07` | `https://source.denx.de/u-boot/u-boot.git` | `ece349ade2973e220f524ce59e59711cc919263f` |

## Update log

- **2026-10-15** v6.12 -> v6.18. The three `v6.12-*` trees were replaced by `v6.18-*`
  (linux-6.18.y, v6.18.56) and deleted. Each new `.config` started from its v6.12
  `.config` (`olddefconfig`), and the debuggability preset was applied over it; every
  preset line survived. Renamed symbols kept their v6.12 answers: CRYPTO_TEST is now
  CRYPTO_BENCHMARK, DRM_I2C_CH7006/SIL164 are DRM_NOUVEAU_CH7006/SIL164, PWM_RCAR and
  PWM_RZ_MTU3 are PWM_RENESAS_RCAR and PWM_RENESAS_RZ_MTU3, PHY_QCOM_SNPS_EUSB2 is
  PHY_SNPS_EUSB2. New gates were opened where v6.12 had the feature on:
  NETFILTER_XTABLES_LEGACY, CPUSETS_V1, LEDS_CLASS_MULTICOLOR, MFD_SEC_I2C (arm64),
  MD_BITMAP. Two arm64 PHY drivers (PHY_EXYNOS5_USBDRD, PHY_FSL_IMX8MQ_USB) are now
  modules, because they depend on TYPEC, which stays a module as in v6.12. The
  `upstream-*` trees were renamed `mainline-*` with their `.config` unchanged, and
  rebuilt from `make clean` so their debug information names the new paths. Trees
  are known by kbuildlab ID, not by name. The guest perf is built from v6.18's
  `tools/perf` (`build-perf.sh`), now with libelf, libdw and libtraceevent.

- **2026-10-11** mainline v7.3-rc5-570 -> v7.3-rc6-386-ga5ebb76233b7; linux-6.12.y
  still v6.12.112. The mainline trees rebuilt incrementally and all seven were
  reindexed (ctags, cscope, gtags, clangd). No Kconfig symbol was added, removed or
  changed in any tree (`.config` before and after), and every preset line survived.
- **2026-10-04** v6.12.111 -> v6.12.112; mainline -> v7.3-rc5-570-ga74306e2e676. The
  mainline trees' `master` tracks `origin/master`; mirror-only refs were pruned. New
  symbols took their defaults: CC_OPT_INLINE_MEMSET (mainline), ARM64_ERRATUM_3821522
  and ARM64_WORKAROUND_BROKEN_AMU_CONSTCNT (upstream-arm64).
- **2026-09-16** v6.12.108 -> v6.12.110; mainline -> v7.3-rc3-60-g587858367581.
  Promptless symbols upstream renamed left the .config: GENERIC_PTDUMP and PTDUMP_CORE
  (now ARCH_HAS_PTDUMP, PTDUMP), MEMORY_HOTPLUG_DEFAULT_ONLINE (now
  MHP_DEFAULT_ONLINE_TYPE_*), CRYPTO_DEV_SUN8I_CE_PRNG (removed). Added on purpose, so
  drgn can read a guest over QMP: FW_CFG_SYSFS in the six 6.12/mainline trees, KEXEC
  (selects VMCORE_INFO) in the two riscv64 trees. Not v4.6-arm64: arm64 gained kexec
  in 4.8 and the fw_cfg vmcoreinfo writer in 4.17.
- **2026-09-06** mainline -> v7.3-rc1-428-g4d7d9486c04d; stable unchanged.
- **2026-09-03** v6.12.106 -> v6.12.108; mainline v7.2-15794 -> v7.3-rc1-101.
- **2026-08-28** first download and verification.
