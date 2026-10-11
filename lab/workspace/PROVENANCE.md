# Kernel source provenance

Every kernel source here came from kernel.org over HTTPS, and each tree's HEAD is
checked against the live `git ls-remote` answer of its origin after every update.

## Sources

- stable (v4.6.7, linux-6.12.y): `https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git`
- mainline (the upstream trees): `https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git`

These are kbuildlab's own defaults (`STABLE_URL`, `MAINLINE_URL` in `bin/kbuildlab`).

## Trees, verified 2026-10-11

| Tree | Ref | Source | Commit (local = kernel.org) |
|---|---|---|---|
| `v4.6-arm64` | tag `v4.6.7` | stable | `096998b11906dd79df5af4d688c9974342dd09f2` |
| `v6.12-x86_64` | `linux-6.12.y` (v6.12.112) | stable | `7aba70ab2e8d8ab3cd45abcaa15e1119a00dc42a` |
| `v6.12-arm64` | `linux-6.12.y` (v6.12.112) | stable | `7aba70ab2e8d8ab3cd45abcaa15e1119a00dc42a` |
| `v6.12-riscv64` | `linux-6.12.y` (v6.12.112) | stable | `7aba70ab2e8d8ab3cd45abcaa15e1119a00dc42a` |
| `upstream-x86_64` | `master` (v7.3-rc6-386) | mainline | `a5ebb76233b79db01e82e061173cb73c6d2b5c6b` |
| `upstream-arm64` | `master` (v7.3-rc6-386) | mainline | `a5ebb76233b79db01e82e061173cb73c6d2b5c6b` |
| `upstream-riscv64` | `master` (v7.3-rc6-386) | mainline | `a5ebb76233b79db01e82e061173cb73c6d2b5c6b` |

The mainline trees move with `kbuildlab update`; v4.6.7 is the last 4.6.y release.

## Why the hashes are enough

- git addresses every object by its content, so equal commit hashes mean equal trees.
- The comparison is against the server's live answer, which anyone can repeat.
- Every fetch is HTTPS.

## Layout

The stable history was fetched once into `.mirrors/linux.git`, with mainline fetched
on top. Each tree's `kernel/` is a local clone of that mirror (hardlinked objects, full
history) whose `origin` is the kernel.org URL above.

## buildroot and u-boot

Pinned to release tags and not moved by kernel updates. The rootfs builds inside the
`kbuildlab:latest` container; early-boot debugging needs no rootfs.

| What | Where | Tag | Source | Commit |
|---|---|---|---|---|
| buildroot | `.buildroot/buildroot` | `2026.02.3` | `https://git.buildroot.net/buildroot` | `679b9ead7620bbf193620d1ebf56f53c1764d37a` |
| u-boot | `firmware/u-boot` | `v2026.07` | `https://source.denx.de/u-boot/u-boot.git` | `ece349ade2973e220f524ce59e59711cc919263f` |

## Update log

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
