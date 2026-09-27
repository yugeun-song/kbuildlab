# 커널 소스 출처 및 무결성 기록 (Provenance)

이 문서는 7종 커널 소스가 **공식 kernel.org에서 보안 연결(HTTPS/TLS)로만** 받아졌음을 증명하기 위한 기록이다.
모든 커밋 해시는 로컬 사본과 **kernel.org 공식 서버의 실시간 `git ls-remote` 응답**을 대조해 일치를 확인했다.

기록 시각: 2026-08-28 (Asia/Seoul)
갱신 시각: 2026-09-03 — `kbuildlab sync` 후 재대조. stable 브랜치는 v6.12.106 → v6.12.108,
mainline은 v7.2-15794 → v7.3-rc1-101 로 전진했다. v4.6.7은 태그라 그대로다.
갱신 시각: 2026-09-06 — `sync-all.sh` 후 재대조. stable은 v6.12.108 그대로,
mainline은 v7.3-rc1-101 → **v7.3-rc1-428-g4d7d9486c04d** 로 전진했다. v4.6.7은 태그라 그대로다.
7종 모두 재빌드하고 ctags/cscope/gtags를 다시 만들었다.

Updated 2026-09-16 — `sync-all.sh`, re-checked against kernel.org with `git
ls-remote`. stable advanced v6.12.108 -> **v6.12.110**; mainline advanced
v7.3-rc1-428 -> **v7.3-rc3-60-g587858367581**; v4.6.7 is a tag and did not move.
All seven rebuilt, and ctags, cscope, gtags and the clangd database remade.

No symbol that already had an answer changed value in any tree. What left the
.config is promptless and upstream renamed it: GENERIC_PTDUMP and PTDUMP_CORE
became ARCH_HAS_PTDUMP and PTDUMP, MEMORY_HOTPLUG_DEFAULT_ONLINE became
MHP_DEFAULT_ONLINE_TYPE_*, and CRYPTO_DEV_SUN8I_CE_PRNG was removed.
PTDUMP_DEBUGFS, DEBUG_WX and every other preset line survived.

Two symbols were then added on purpose, additively, so drgn can read a guest
over QEMU's QMP monitor: CONFIG_FW_CFG_SYSFS in all six 6.12/mainline trees and
CONFIG_KEXEC in the two riscv64 ones, which selects VMCORE_INFO. Together they
make the guest write the note that `-device vmcoreinfo` advertises. No existing
answer was rewritten to do it. v4.6-arm64 is excluded: arm64 gained kexec in 4.8
and the fw_cfg vmcoreinfo writer in 4.17, so neither symbol can do anything
there, and that tree's .config is byte-for-byte what it was.

## 사용한 공식 URL (전부, 예외 없음)

- **stable 트리** (v4.6.7, v6.12.y 소스): `https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git`
- **mainline 트리** (upstream 소스, 리누스 트리): `https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git`

두 URL은 kbuildlab 자체가 하드코딩해 쓰는 기본값과 동일하다 (`bin/kbuildlab`의 `STABLE_URL`, `MAINLINE_URL`).

## 7종 커널 ↔ 출처 ↔ 검증된 커밋 해시

| 트리 폴더 | 버전/ref | 출처 URL | 커밋 SHA (로컬=공식 실측 일치) |
|---|---|---|---|
| `v4.6-arm64` | tag `v4.6.7` | stable | `096998b11906dd79df5af4d688c9974342dd09f2` |
| `v6.12-x86_64` | branch `linux-6.12.y` (=v6.12.110) | stable | `fa5b06866b643cce7072a4e7c6be7ee3f0f1b95c` |
| `v6.12-arm64` | branch `linux-6.12.y` (=v6.12.110) | stable | `fa5b06866b643cce7072a4e7c6be7ee3f0f1b95c` |
| `v6.12-riscv64` | branch `linux-6.12.y` (=v6.12.110) | stable | `fa5b06866b643cce7072a4e7c6be7ee3f0f1b95c` |
| `upstream-x86_64` | mainline `master` (HEAD) | torvalds | `587858367581b9c55c3690f4e63382ad622719d4` |
| `upstream-arm64` | mainline `master` (HEAD) | torvalds | `587858367581b9c55c3690f4e63382ad622719d4` |
| `upstream-riscv64` | mainline `master` (HEAD) | torvalds | `587858367581b9c55c3690f4e63382ad622719d4` |

> upstream 세 개는 시점에 따라 mainline HEAD가 움직인다. 위 해시는 클론 시점의 값이며, `kbuildlab update`로 갱신 시 최신 mainline으로 fast-forward된다.

## 무결성 보장 방식

1. **콘텐츠 주소화(SHA):** git의 모든 오브젝트는 내용 기반 해시다. 소스가 한 바이트라도 변조되면 위 커밋 해시가 달라진다. 위 해시가 공식 서버 값과 같다는 것은 트리 전체가 공식본과 동일함을 의미한다.
2. **실시간 공식 서버 대조:** 로컬 사본의 해시를 `git ls-remote <공식 URL>` 실측 결과와 직접 비교(위 표). "믿어달라"가 아니라 재현 가능한 검증.
3. **전송 보안:** 모든 fetch는 HTTPS(TLS)로만 수행. 평문/비보안 경로 없음.

## 다운로드 구조 (숨김 없음)

- 커널을 7번 따로 내려받지 않았다. 공식 stable 트리를 **미러 1개**(`.mirrors/linux.git`, 전체 이력 14,959,563 오브젝트)로 받고, upstream용으로 공식 mainline 최신분을 추가 fetch했다.
- 7개 트리의 `kernel/`은 이 공식 미러에서 **로컬 클론(오브젝트 하드링크)**으로 만들어 전체 이력을 각자 보존한다. 각 트리의 `origin`은 위의 해당 공식 kernel.org URL로 설정된다.
- 이유: 동일 출처를 7번 재다운로드해도 보안상 이득이 없고 대역폭만 낭비(~35GB). 대신 각 트리 해시를 공식 서버와 대조해 진위를 보장한다.

## buildroot·u-boot (rootfs / 펌웨어)

커널 외의 두 소스도 공식 출처에서만 받았고, 둘 다 **릴리스 태그에 고정**되어 있다.
`sync-all.sh`는 커널 7종만 전진시키므로 이 둘은 의도적으로 움직이지 않는다.

| 대상 | 위치 | 태그 | 출처 URL | 커밋 SHA |
|---|---|---|---|---|
| buildroot | `.buildroot/buildroot` | `2026.02.3` | `https://git.buildroot.net/buildroot` | `679b9ead7620bbf193620d1ebf56f53c1764d37a` |
| u-boot | `firmware/u-boot` | `v2026.07` | `https://source.denx.de/u-boot/u-boot.git` | `ece349ade2973e220f524ce59e59711cc919263f` |

- rootfs 빌드는 호스트가 아니라 `kbuildlab:latest` 컨테이너 안에서 돈다(Arch 호스트와 buildroot의 충돌 회피).
- early-boot gdb 디버깅은 rootfs 없이도 동작한다(게스트가 리셋 벡터에서 정지, 디버거 부착).
