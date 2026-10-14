# Linux 커널 리서치 환경 구축 보고서

**작성:** 2026-08-28 · **개정:** 2026-10-15(v6.12 → v6.18, upstream → mainline, 트리 이름 → kbuildlab ID, §13) · **위치:** `~/workspace/linux-kernel-research/`
**도구:** kbuildlab(빌드/QEMU/GDB 드라이버) · gdbtools(GDB 조기부팅 심볼라이저) · nvim-config(nvim-dap 커널 디버깅)

관측 가능성(observability)과 조기부팅 디버깅 정보 극대화를 목표로, 7종 64비트 리눅스 커널을
전체 git 이력과 함께, 최대 디버그 설정으로, KASLR을 켜고도 head.S부터 단계별로 디버깅
가능하게 구축했다. 아래는 그 산출물·검증 결과·사용법의 종합이다.

---

## 1. 7종 커널 (전부 빌드 완료)

| 트리 폴더 | kbuildlab ID | 버전 | arch | git ref | 커밋 | vmlinux | 부트 이미지 |
|---|---|---|---|---|---|---|---|
| `v4.6-arm64` | `2fc57a9a83d7` | v4.6.7 (2016) | arm64 | tag `v4.6.7` | `096998b1` | 219M | Image 25M |
| `v6.18-x86_64` | `0c5b6d04f97a` | v6.18.56 | x86_64 | branch `linux-6.18.y` | `17ad2612` | 476M | bzImage 20M |
| `v6.18-arm64` | `2349b212ea4a` | v6.18.56 | arm64 | branch `linux-6.18.y` | `17ad2612` | 535M | Image 57M |
| `v6.18-riscv64` | `805a3e5c570c` | v6.18.56 | riscv64 | branch `linux-6.18.y` | `17ad2612` | 940M | Image 47M |
| `mainline-x86_64` | `afce473b9a94` | v7.3-rc6-386 (mainline) | x86_64 | `master` | `a5ebb762` | 510M | bzImage 21M |
| `mainline-arm64` | `3bef7c6ecd23` | v7.3-rc6-386 (mainline) | arm64 | `master` | `a5ebb762` | 541M | Image 58M |
| `mainline-riscv64` | `3367ece228e6` | v7.3-rc6-386 (mainline) | riscv64 | `master` | `a5ebb762` | 959M | Image 47M |

- 큰 vmlinux(수백 MB)는 풀 DWARF5 디버그 정보의 결과다(성능이 아니라 관측성 우선).
- 트리 폴더에는 `kernel/`(소스+빌드 산출물)만 있다. 머신 기술 `tree.conf`는 kbuildlab 레지스트리 `~/.config/kbuildlab/trees/<ID>/`에, 부팅 디스크·/persist 이미지·모듈 아카이브는 `~/.local/share/kbuildlab/trees/<ID>/`에 둔다. 명령은 트리를 ID(앞 4자리 이상) 또는 경로로 가리킨다.
- **전체 git 이력 보존**: 각 트리 148만+ 커밋. `git log`/`blame`/`Link:`(→lore.kernel.org LKML)·`Fixes:`·`Reviewed-by:` 추적으로 소스만으로 LKML 수준 정보 획득 가능.

## 2. 출처·보안 (공급망)

- **공식 kernel.org에서만, HTTPS로만** 받았다. 상세·검증 기록: `PROVENANCE.md`.
  - stable(v4.6.7, v6.18.y): `https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git`
  - mainline: `https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git`
- 공식 stable 트리를 **미러 1개**(`.mirrors/linux.git`, 전체 이력)로 받고, 7개 트리는 이 미러에서 클론했다. 트리마다 객체를 따로 두어서 미러나 다른 트리와 하드링크·alternates를 공유하지 않는다(2026-10-15 정리). 그래서 한 트리를 패치하거나 지워도 다른 트리에 영향이 없다. 각 트리 `origin`은 위 공식 URL.
- **무결성:** 각 트리 커밋 해시를 `git ls-remote` 실시간 공식 서버 응답과 대조해 일치 확인(재현 가능). git은 콘텐츠 주소화라 1바이트 변조도 해시가 달라진다.
- buildroot(rootfs용)도 공식만: `https://git.buildroot.net/buildroot` (tag 2026.02.3 LTS), 소스는 buildroot가 업스트림에서 해시 검증하며 받음.

## 3. 빌드 환경 (Ubuntu 컨테이너, #4)

Arch 호스트(gcc-16)로는 구형 커널·buildroot가 깨지므로 전부 컨테이너에서 빌드하고 산출물만 호스트에 보존.

| 이미지 | 베이스 | 툴체인 | 용도 |
|---|---|---|---|
| `kbuildlab:latest` | Ubuntu 24.04 | gcc-13 (x86/arm), **gcc-14 (riscv)** | 모던 6종 |
| `kbuildlab-legacy:latest` | Debian 9 stretch | gcc-6.3 | v4.6 전용(#10) |

- riscv만 gcc-14: mainline riscv 동적 ftrace가 `-fmin-function-alignment`(gcc-14+) 요구.
- v4.6는 gcc-13으로 빌드 불가 → Debian stretch(gcc-6.3, v4.6 동시대)로 처리.
- 컨테이너는 `--user`로 호스트 사용자 권한 실행 → 산출물이 root가 아닌 **본인 소유**.
- pahole(BTF), ctags/cscope/GNU GLOBAL(gtags) 포함.

## 4. 디버그 설정 (최대 관측성, #2)

공유 프리셋(`kbuildlab/presets/`) 기반. 핵심(모던 6종, 107줄 검증 통과):
- **디버그 정보:** DWARF5(`.debug_loclists/rnglists` — 변수가 PC 구간별 어느 레지스터에 사는지까지), REDUCED/SPLIT 금지(`<optimized out>` 최소화), BTF(drgn/bpftrace/CO-RE용, host bpftool 파싱 검증), GDB_SCRIPTS(lx-*).
- **심볼:** KALLSYMS_ALL, VMLINUX_MAP, not stripped(.symtab). vmlinux 심볼 22만+.
- **트레이싱:** 전체 ftrace(function/graph/retval/stack/irqsoff/sched/osnoise/timerlat), kprobe/uprobe(+BTF args), perf, BPF(JIT/LSM/preload).
- **디버깅:** KGDB, DEBUG_FS, MAGIC_SYSRQ, 프레임포인터(arm64 강제/x86 unwinder/riscv 명시), x86 PTI off(gdb CR3 일치), 재현성 위해 RANDOMIZE_MEMORY off.
- **KASLR:** `CONFIG_RANDOMIZE_BASE=y`로 빌드(단일 빌드가 두 모드 지원) — 부팅 시 `nokaslr`이면 결정적, 없으면 KASLR 활성(gdbtools가 슬라이드 보정). 검증: 7종 전부 `RANDOMIZE_BASE=y`.

**버전별 특수 처리(#10):**
- **v4.6-arm64:** 6.12 프리셋 심볼 상당수 부재 → v4.6 전용 `config-fragment`(30줄). arm64 kprobes는 v4.8, uprobes는 이후에야 병합 → v4.6엔 없음(진짜 한계, perf/BPF는 존재).
- **mainline-riscv64:** mainline riscv에서 (a) ftrace가 !PREEMPTION에서 분리+gcc-14 요구, (b) KASLR이 RELOCATABLE 요구, (c) 컴파일타임 PREEMPT_NONE 불가(부팅 `preempt=none`으로 대체) → `presets/arch/riscv64-mainline.config` 변형으로 처리(98줄 통과).

## 5. ★ 핵심: 조기부팅 다단계 + KASLR 디버깅 (실검증 완료)

head.S 최초 명령(MMU off, 물리주소)부터 → MMU 켜지는 순간 → 물리→고VA 전이(애매한 상태) →
고VA 가동까지, **하나의 연속 세션에서** 단계별로 밟으며 심볼라이제이션이 오동작 없이 유지되는지,
**KASLR을 켜고도** 되는지 실제 부팅으로 검증했다. gdbtools의 arch/버전 무관 opcode 스캔이
핵심(하드코딩 주소 없음).

**검증 매트릭스 (2026-08 실측, 당시 트리 이름. v6.18·mainline 재검증은 §13):**

| 커널 | 다단계 regime walk | KASLR 슬라이드 복구 |
|---|---|---|
| v4.6-arm64 (레거시) | ✅ crossing 앵커 `__enable_mmu:br→__mmap_switched` | ✅ slide `0x3a1400000`, start_kernel@init/main.c:484 |
| v6.12-arm64 | ✅ 5단계 `_text→__enable_mmu→__primary_switch→__primary_switched→start_kernel` | ✅ slide `0x527879200000`, start_kernel@870 |
| v6.12-riscv64 | ✅ satp regimes + `kx`(물리 PC examine) + `kpt` 정직한 거절 | (메커니즘 동일) |
| v6.12-x86_64 | ✅ TCG 부팅 + regime(startup_64/start_kernel) | ✅ 디컴프레서 복구(phys base) → 크로싱에서 슬라이드 `0x28e00000`, start_kernel hit |
| upstream-arm64 (mainline v7.2) | ✅ 버전 무관 opcode 스캔 | ✅ slide `0x41ee1f200000`, start_kernel@983 |

각 단계에서 확인된 것(v6.12-arm64 실측):
1. `_text`(MMU off, PC 물리) → pre-MMU shadow 심볼로 물리주소도 심볼화
2. `__enable_mmu+56`(MMU off, 물리) → MMU 켜지기 직전
3. `__primary_switch`(**MMU on이지만 PC 아직 물리** — 님이 말한 애매한 전이) → `kpt` 하드웨어 페이지테이블 워크(TTBR0 idmap) 정상
4. `__primary_switched`(MMU on, PC 고VA) → 네이티브 가상 심볼화

**KASLR:** `b start_kernel; continue`만으로 crossing catcher가 물리→고VA 전이 명령에서 실제 랜덤
슬라이드를 읽어 전 심볼을 런타임 VA로 재배치 → start_kernel이 shifted VA에서 clean hit (링크VA+슬라이드 교차검증 일치).

**주요 gdbtools 명령:** `kearly bootbreak/where/mmu/regimes[walk]/kaslr/overmmu`, `kp2v/kv2p/addr2sym/kx`,
`kb/kw`(regime 인지 브레이크/워치), `kpt/kpgd/kpthex`(페이지테이블), `kcensus/ksregs`(제어·시스템 레지스터 전수),
`mmview`(커널 vmmap), `kdtb`(FDT 덤프), `cfgdis`(분기 화살표 디스어셈).

## 6. 사용법

전제: docker 그룹 로그인 상태(재로그인 후). `kbuildlab`을 PATH에 넣거나 전체 경로 사용.
워크스페이스는 이미 등록됨(`kbuildlab init --workspace` 완료).

**터미널 디버깅(2개 창):**
```
# 창1: 게스트를 리셋 벡터에서 정지시켜 부팅
kbuildlab run 2349                        # v6.18-arm64, nokaslr(결정적)
kbuildlab run 2349 --kaslr                # KASLR 활성
kbuildlab run 0c5b --no-kvm               # v6.18-x86_64. x86 조기부팅은 TCG 권장(HW 브레이크 결정성)
#   같은 커널 여러 개: 기본 포트가 차면 자동으로 다음 포트 사용(안내 출력), --port N으로 지정도 가능

# 창2: gdb(pwndbg + gdbtools) 부착 — 기본적으로 head.S _text(커널 최초 명령)에 자동 정지
kbuildlab attach 2349                 # 연결 즉시 kearly bootbreak → _text (--no-bootbreak로 raw)
(gdb) kearly regimes walk    # MMU 정지점 전부 무장
(gdb) continue               # 반복 -> entry→mmuon→crossing→virtual→start 순서로 통과
(gdb) kearly where / kearly mmu / kpt / addr2sym $pc / mmview / kcensus ...
# KASLR: (gdb) b start_kernel ; continue   # 슬라이드 자동 보정 후 hit
```
`[TREE]` 인자는 트리 ID(앞 4자리 이상, `kbuildlab trees`가 보여 준다)와 경로를 받고, **커널 소스 최상위 폴더 자체**(`.../kernel`, 이름 임의)도 받는다(내용 기반 인식).

**완전한 OS로 부팅 + 게스트 안에서 작업:**
```
kbl run 2349 --run                   # frozen 없이 곧장 완성된 OS로 부팅
# guest 로그인: root / root
guest$ ip a; ping -c1 8.8.8.8; perf stat ls; strace -c ls; tcpdump -c3 -i eth0
host$  ssh -p 2222 root@localhost    # (pw: root) 또는 nvim scp://root@localhost:2222//root/x.c
host$  clang -O2 -target bpf -c prog.bpf.c -o prog.o   # eBPF는 호스트에서(BPF는 arch무관)
       scp -P 2222 prog.o root@localhost:  →  guest$ bpftool prog load prog.o /sys/fs/bpf/x
guest$ gdb ./injected_binary         # in-guest 유저스페이스 디버깅(gdb 15.2)
```

**nvim-dap 디버깅:** 게스트를 `kbuildlab run`으로 띄운 뒤 nvim에서 `<leader>dq`(또는 `:DbgKernel`).
실행 중 QEMU를 자동 발견(`/proc` 스캔) → arch에 맞는 gdb+gdbtools+pwndbg로 부착. `:DbgTargets`로 후보 확인,
`:DbgKernelEarly`로 조기부팅 머신 무장.
**실검증(2026-08, v6.12-arm64):** headless nvim으로 게스트에 DAP 부착 → 세션 초기화 성공(`DAP_INIT OK`).
호환성: 3개 arch gdb 모두 GDB 17.2+Python+DAP(`-i dap`) 지원 확인. nvim은 세션을 `kbuildlab attach --list --json`과
`kbuildlab attach ID --json`에서 받고, 트리는 ID로 넘긴다.

**갱신·재색인(#12):** `./sync-all.sh` — 전 트리 소스 fetch+ff → 증분 재빌드(.config 유지) → ctags/cscope/gtags 재색인 한 번에. 특정 트리만: `./sync-all.sh <ID...>`.

## 7. 코드 탐색 (clangd off, #3 #7)

- 각 트리: **ctags + cscope + GNU GLOBAL(gtags)** 색인 생성(빌드한 arch만). clangd 대신 이 3중 인덱스.
- nvim: 연구 폴더 전체에서 **clangd 자동 비활성화** 규칙 등록(`~/.local/share/nvim/lsp_filter/rules.json`, `within=연구폴더`) → 오탐 없음.

## 8. 게스트 = 완전한 개발 OS (rootfs) — 3종 arch 실검증 완료

`kbl run <tree>`는 커널만 올리는 게 아니라 **로그인·네트워크·관측·편집·디버깅이 되는 완성된 OS**로 부팅된다.
공식 buildroot 2026.02.3에서 빌드(`build-rootfs.sh <arch>`, defconfig `rootfs/defconfig-*`).
rootfs는 arch별로 공유(같은 arch의 v6.18/mainline/v4.6 트리가 같은 cpio를 `INITRD=`로 참조).

**`kbl run` 기본값(옵션 없이) = 진정한 OS + 조기부팅 디버거 대기, 전부 오버라이드 가능:**

| 기본 | 설명 | 끄기/바꾸기 |
|---|---|---|
| rootfs 탑재 | tree.conf `INITRD=`, 로그인 가능 | `--no-initrd` / `--initrd PATH` |
| gdb 대기(frozen) | `-S`, head.S 첫 명령에서 정지 | `--run`(즉시 부팅) |
| KVM off (TCG) | 결정적 조기부팅 HW 브레이크 | `--kvm`(x86) |
| KASLR off | nokaslr | `--kaslr` |
| 네트워크 on | user/virtio, DHCP 10.0.2.15 | `--no-net` |
| SSH 포워딩 | host `-p 2222` → guest 22 | `--ssh-port N` |

**실검증 (2026-10-15, v6.18 각 arch를 새 rootfs로 부팅, 같은 rootfs로 mainline 3종·v4.6도 확인):**

| 항목 | x86_64 | arm64 | riscv64 |
|---|---|---|---|
| root 로그인 (pw: `root`), TERM=xterm-256color | ✅ | ✅ | ✅ |
| 콘솔 크기 = 호스트 터미널, 창 크기 변경을 따라감 | ✅ | ✅ | ✅ |
| 네트워크: eth0 DHCP, `ping 8.8.8.8`, DNS | ✅ | ✅ | ✅ (virtio-mmio NIC) |
| 호스트 → 게스트 `ssh` | ✅ | ✅ | ✅ |
| perf 6.18: stat / record `-g` / DWARF 콜스택 / trace / probe(커널·`-x` 유저) | ✅ | ✅ | ✅ |
| bpftool 7.5 / libbpf / BPF JIT / BTF | ✅ | ✅ | ✅ |
| strace / tcpdump | ✅ | ✅ | ✅ |
| ftrace(tracefs·debugfs 자동 마운트) / kprobe 이벤트 | ✅ | ✅ | ✅ |
| uftrace | ✅ | ✅ | ❌ (buildroot riscv 미지원) |
| ltrace | ✅ | ✅ | ❌ (buildroot riscv 미지원) |
| vim 9.1 / bash 5.2 / python3 3.14 | ✅ | ✅ | ✅ |
| **gdb 15.2 / gdbserver 15.2** (게스트 안 디버깅, gdbserver 원격 세션) | ✅ | ✅ | ✅ |
| 호스트 `kbuildlab drgn` | ✅ | ✅ | △ (drgn 0.2.0이 riscv64 커널 VA를 변환하지 못함) |

- **v4.6-arm64:** 위 항목 중 kprobe·uprobe·BTF는 커널에 없다(원래 한계). perf 6.18은 4.13 미만 커널의 기능 탐지 순서 버그를 고친 패치(`rootfs/perf-0001-*.patch`)로 빌드해 record·trace까지 동작한다. 게스트 gdb가 clone3(435)를 부를 때마다 4.6의 `do_ni_syscall`이 레지스터 덤프를 콘솔에 찍지만 동작에는 영향이 없다(rseq와 같은 종류). 호스트 drgn은 vmcoreinfo가 없어(4.6 arm64에 fw_cfg vmcoreinfo 기록기가 없음) 붙지 못한다.

- **neovim 원격 편집:** `ssh -p 2222 root@localhost`(pw `root`) 또는 `nvim scp://root@localhost:2222//path`. 호스트→게스트 SSH 로그인 실증(`ssh-verify.py`, SSH_ASKPASS force). 호스트 공개키가 있으면 자동으로 authorized_keys 주입(무암호).
- **eBPF/C 개발(호스트 컴파일 방식):** eBPF는 호스트 `clang -target bpf`(BPF 바이트코드는 arch 무관)로 빌드 → 게스트에 주입 → `bpftool prog load`. 일반 C는 호스트 크로스 gcc로 빌드해 주입. 게스트엔 bpftool + libbpf + 커널 BTF(`/sys/kernel/btf/vmlinux`) 존재. **게스트 안 gcc/g++/clang 컴파일은 buildroot 구조상 불가**(호스트 크로스 전용); 단 타겟 libstdc++ 런타임이 있어 호스트-빌드 C++ 바이너리 실행·gdb 디버깅은 됨.
- **post-build(`rootfs/post-build.sh`, 3종 공통):** libsframe 복사(bpftool 의존), perf 복사(`rootfs/perf-<arch>`, `build-perf.sh TREE`가 그 트리의 `tools/perf`로 빌드), eth0 DHCP stanza(증분빌드 대비), sshd root 로그인 허용, 호스트 pubkey 주입, tracefs·debugfs fstab 항목, 콘솔 크기 서비스 `S12winsize`.
- **미포함:** 게스트 안 drgn(buildroot 패키지 없음, 호스트 `kbuildlab drgn`으로 붙는다), 게스트 네이티브 gcc/clang/neovim(구조상 불가 → 호스트에서).

## 9. kbuildlab / gdbtools 수정 내역 (워킹트리만, **커밋/푸시 안 함**)

**gdbtools (session.py 3곳):** KASLR 슬라이드 fallback 추가 — 크로싱 직후 고VA에서 fresh attach 시(기존 앵커 kimage_voffset/kernel_map.virt_addr가 아직 0) 슬라이드를 복구 못 하던 틈을 메움. 새 메서드 `_slide_via_pc_pa()`가 $pc를 하드웨어 page-table walk(`pagewalk`, slide 무관, CPU resume 없음)로 물리주소 PA를 얻어 `slide = pc − PA + offset`을 비순환 계산(고정 물리로드 arm64/riscv; `_is_va(pc)`+이미지 멤버십 가드로 x86·물리단계는 미도달). `kearly kaslr auto` 흐름과 on-stop 자동적용 훅에 연결. arm64 실측 검증(참값 정확 일치), 회귀 없음.

**kbuildlab은 아래를 수정(전부 되돌릴 수 있음):**
- `bin/kbuildlab`: full-history sync/fetch(얕은 클론 방지), 컨테이너 `--user`+레포 ro 마운트(소유권/프리셋경로 버그 수정), `image legacy` 변형, tags에 gtags, init이 `qemu.conf` 심볼릭 링크 생성, mainline arch 프리셋 선택.
- `lib/run-qemu.sh`: KASLR 토글(`--kaslr`/`--no-kaslr`), 포트 자동 회피(다중 실행), x86 TCG 시 `-cpu host`→`max`.
- `lib/run-gdb.sh`: x86 KASLR용 decompressor vmlinux 경로 전달(nvim 어댑터와 parity).
- `containers/Containerfile`: gtags용 `global`, riscv gcc-14.
- 신규: `containers/Containerfile.legacy`, `presets/arch/riscv64-mainline.config`.
- 발견·수정한 실제 버그 2건: ① 얕은 클론으로 이력 잘림 → full-history 감지식, ② 프리셋 경로가 컨테이너에 안 보여 config 실패 → 레포 ro 마운트.

## 10. 알려진 한계

- **x86 KASLR 조기부팅:** 동작 확인됨(run-gdb가 `GDBTOOLS_X86_DECOMP_PA=0x100000` 자동설정). 단 결정적 HW 브레이크 위해 **TCG(`--no-kvm`) 필수**이고, 디컴프레서가 400MB 디버그 커널을 TCG로 압축해제하므로 **1~3분 소요**(arm/riscv은 물리로드 고정이라 빠름). 순서: `kearly bootbreak`(물리베이스 복구) → `kearly kaslr auto`(가상 슬라이드).
- **KASLR "어느 단계에서 붙어도":** 리셋 walk(전 단계) · 실행 중 fresh attach(kimage_voffset) · **크로싱 직후 고VA fresh attach(예전 틈)** 모두 자동 복구. 예전 틈은 gdbtools에 fallback 추가로 해결(9절): 고VA 정지에서 $pc의 하드웨어 물리주소로 `slide = pc − PA(pc) + offset`을 비순환 계산. arm64 실측 검증(fallback값이 kimage_voffset 참값과 정확 일치), 회귀 없음(물리단계 None, x86/cold-frozen 무영향).
- **v4.6-arm64:** kprobes/uprobes 부재(arch 지원이 v4.8+). Python 3.14에서 v4.6의 lx 스크립트가 SyntaxWarning(무해).
- **drgn:** buildroot rootfs 미포함(위 8절).
- **RAM 13GiB:** 풀 디버그 빌드는 `-j5`로 순차 진행(동시 빌드 시 OOM 위험).

## 10.5 테스트 스위트 (엄격 유닛+e2e, 병렬)

`logs/run-tests.sh` — 각 테스트가 자기 QEMU만 띄우고(고유 포트, `--smp 1`) 자기 PID만 정리, 동시 4개(~4코어)로 병렬. **37/37 PASS, 0 FAIL** (재실행 가능: `bash logs/run-tests.sh`, 결과 `logs/test-results.tsv`). 2026-08 v6.12 트리에서 처음 통과했고, 2026-10-15 v6.18·mainline·v4.6 트리(ID로 지정)에서 다시 37/37을 확인했다(§13).

- **kaslr_fb_arm64 (6):** `_slide_via_pc_pa` — 물리 regime에서 None, virtual에서 값==kimage_voffset 참값, 40회 연타·garbage 주소에도 무크래시.
- **KASLR e2e:** arm64 on(shifted VA, slide≠0)/off(link VA, slide=0), x86 on(디컴프레서 베이스+슬라이드+hit)/off, mainline, v4.6(레거시), riscv — 총 11 assertion.
- **commands_arm64 (11):** kw 발화·sym·kpt·kx·kv2p(regime 인지)·ksregs·kcensus·mmview·koff·cfgdis·kdtb.
- **run-qemu 엣지:** 포트 자동회피, x86 TCG `-cpu max` 부팅, KASLR 토글 cmdline.

테스트 중 발견·수정한 것은 전부 **테스트 하네스 자체 버그**였음(실기능 아님): ① `local a=$1 b=$..a..` 한 줄 선언의 `set -u` unbound(→선언 분리), ② riscv start_kernel이 소스라인 없이 뜨는 걸 못 맞춘 과잉 grep(→패턴 완화).

## 11. 자원·정리 (#15)

- 디스크: 연구 폴더 실사용 ~64GiB(btrfs zstd 압축 + git 하드링크 덕에 논리크기보다 작음), 411GiB 여유.
- QEMU/docker는 **띄운 것만 PID/컨테이너ID로 표적 정리**(일괄 kill 금지). 위 검증들도 각 QEMU를 본인 PID로만 종료 확인.
- 빌드 드라이버는 독립 프로세스 그룹(PGID 기록)으로 실행해 필요 시 그 그룹만 정지.

## 12. 파일 배치

```
~/workspace/linux-kernel-research/
  <7개 트리>/kernel/        커널 소스+산출물(vmlinux, 부트이미지, tags/cscope.out/GTAGS)
  <7개 트리>/tree.conf      머신 기술 (qemu.conf는 이 파일로의 심볼릭 링크)
  .mirrors/linux.git        공식 stable 전체이력 미러(트리들이 하드링크 공유)
  .buildroot/buildroot      공식 buildroot 2026.02.3
  rootfs/{defconfig-*, <arch>/images/rootfs.cpio.gz}
  sync-all.sh               전 트리 갱신+재빌드+재색인
  build-rootfs.sh           arch별 rootfs 빌드
  PROVENANCE.md, REPORT.md
  logs/                     빌드/검증 로그
```

## 13. 2026-10-15 개정: v6.18, mainline, 트리 ID

**바뀐 것**
- **v6.12 → v6.18.** `v6.12-*` 3종을 지우고 `v6.18-*`(linux-6.18.y, v6.18.56)를 새로 클론했다. 각 `.config`는 v6.12 `.config`에서 출발해 `olddefconfig`와 디버그 프리셋을 거쳤고, 프리셋 줄은 모두 살아남았다. 이름이 바뀐 심볼은 v6.12 답을 그대로 옮겼다(CRYPTO_TEST → CRYPTO_BENCHMARK 등, 목록은 `PROVENANCE.md`).
- **upstream → mainline.** 폴더와 트리 이름만 바꾸고 `.config`는 그대로 두었다. 디버그 정보가 새 경로를 가리키도록 `make clean` 후 전부 다시 빌드했다(`DW_AT_comp_dir`, `vmlinux-gdb.py` 확인).
- **트리 ID.** kbuildlab은 트리를 이름이 아닌 16자리 무작위 ID로 구분한다. 명령에는 앞 4자리 이상을 쓴다. 표시는 12자리이고, 다른 트리와 겹치면 더 길어진다. `kbuildlab init`은 소스를 받을 디렉터리를 반드시 인자로 받는다.
- **하드링크 제거.** 7종 트리가 미러·다른 트리와 공유하던 git pack 하드링크를 끊었다. 이제 트리마다 자기 사본만 쓴다.
- **게스트 rootfs.** gdbserver 추가, 콘솔 크기 자동 동기화(`S12winsize` + 호스트 `lib/winsize.py`), getty TERM을 xterm-256color로 변경, tracefs·debugfs 자동 마운트, libtraceevent 추가. perf는 v6.18 `tools/perf`로 libelf·libdw·libtraceevent와 함께 빌드한다(`build-perf.sh`). 이전 빌드 스크립트의 `NO_LIBELF=0`은 perf Makefile의 `ifndef` 검사 때문에 오히려 libelf를 끄고 있었으므로 뺐다.
- **kbuildlab 수정.** SSH·QMP 포트와 persist 디스크를 GDB 포트처럼 O_EXCL claim으로 잡게 했다(같은 트리·여러 트리를 동시에 띄울 때의 경쟁 조건 해소). `kbuildlab trees`가 트리 정보를 병렬로 모으게 해서, 표 출력 시간이 0.4~1.0초에서 0.13초로 줄었다.
- **gdbtools 수정.** `kearly regimes stop`이 이미 그 지점에 있으면 continue하지 않는다. x86에서 bootbreak를 다시 불렀을 때 직전에 세운 커널 엔트리에 있으면 EFI stub 단계를 다시 밟지 않는다. 호스트 gdb가 18.1로 올라가면서 `aarch64-linux-gnu-gdb` 17.2의 Python이 gdb-common 18.1과 맞지 않게 되었고, kbuildlab은 그 gdb를 건너뛰고 일반 gdb로 붙는다.

**재검증 (2026-10-15)**

| 검사 | 범위 | 결과 |
|---|---|---|
| `logs/full-verify.sh`: 진입점 심볼·`x/i`·레지스터·`si`·`start_kernel` 브레이크·bt·watchpoint·`lx-version` | 7종 전부 | 56/56 PASS |
| `logs/run-tests.sh`: KASLR e2e·fallback·명령·포트 자동 회피·x86 TCG·riscv | v6.18·mainline·v4.6 | 37/37 PASS |
| 게스트 도구(§8 표) | v6.18 3종, mainline 3종, v4.6 | §8 참고 |
| 콘솔 크기: 로그인 시 50×200, 창 변경 40×150 → 45×177, 재로그인 후 유지 | 7종 전부 | PASS |
| 게스트 3개 동시 실행: SSH 포트 2222/2223/2224 자동 배정 | v6.18 3종 | PASS |

- 테스트 하네스 쪽 수정 한 건: `commands_arm64.kpt`의 기대 문구. gdbtools의 `kpt`는 이제 MMU가 꺼진 상태에서 페이지 테이블을 덤프하지 않고 그 이유를 설명하므로, 그 설명도 정상 응답으로 받는다.

