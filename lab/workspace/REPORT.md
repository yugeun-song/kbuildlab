# Linux 커널 리서치 환경 구축 보고서

**작성:** 2026-08-28 · **위치:** `~/workspace/linux-kernel-research/`
**도구:** kbuildlab(빌드/QEMU/GDB 드라이버) · gdbtools(GDB 조기부팅 심볼라이저) · nvim-config(nvim-dap 커널 디버깅)

관측 가능성(observability)과 조기부팅 디버깅 정보 극대화를 목표로, 7종 64비트 리눅스 커널을
전체 git 이력과 함께, 최대 디버그 설정으로, KASLR을 켜고도 head.S부터 단계별로 디버깅
가능하게 구축했다. 아래는 그 산출물·검증 결과·사용법의 종합이다.

---

## 1. 7종 커널 (전부 빌드 완료)

| 트리 폴더 | 버전 | arch | git ref | 커밋 | vmlinux | 부트 이미지 |
|---|---|---|---|---|---|---|
| `v4.6-arm64` | v4.6.7 (2016) | arm64 | tag `v4.6.7` | `096998b1` | 172M | Image 18M |
| `v6.12-x86_64` | v6.12.106 | x86_64 | branch `linux-6.12.y` | `8b74a2fb` | 391M | bzImage 16M |
| `v6.12-arm64` | v6.12.106 | arm64 | branch `linux-6.12.y` | `8b74a2fb` | 485M | Image 61M |
| `v6.12-riscv64` | v6.12.106 | riscv64 | branch `linux-6.12.y` | `8b74a2fb` | 700M | Image 33M |
| `upstream-x86_64` | v7.2-15794 (mainline) | x86_64 | `master` | `1b78070a` | 437M | bzImage 18M |
| `upstream-arm64` | v7.2-15794 (mainline) | arm64 | `master` | `1b78070a` | 533M | Image 68M |
| `upstream-riscv64` | v7.2-15794 (mainline) | riscv64 | `master` | `1b78070a` | 806M | Image 36M |

- 큰 vmlinux(수백 MB)는 풀 DWARF5 디버그 정보의 결과다(성능이 아니라 관측성 우선).
- 각 트리 안: `kernel/`(소스+빌드 산출물, 원래 이름 보존), `tree.conf`(머신 기술), `qemu.conf`(→tree.conf 심볼릭 링크, nvim용), 필요시 `config-fragment`.
- **전체 git 이력 보존**: 각 트리 148만+ 커밋. `git log`/`blame`/`Link:`(→lore.kernel.org LKML)·`Fixes:`·`Reviewed-by:` 추적으로 소스만으로 LKML 수준 정보 획득 가능.

## 2. 출처·보안 (공급망)

- **공식 kernel.org에서만, HTTPS로만** 받았다. 상세·검증 기록: `PROVENANCE.md`.
  - stable(v4.6.7, v6.12.y): `https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git`
  - mainline(upstream): `https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git`
- 공식 stable 트리를 **미러 1개**(`.mirrors/linux.git`, 전체 이력)로 받고, 7개 트리는 이 미러에서 로컬 하드링크 클론(전체 이력 각자 보존, 디스크 절약). 각 트리 `origin`은 위 공식 URL.
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
- **upstream-riscv64:** mainline riscv에서 (a) ftrace가 !PREEMPTION에서 분리+gcc-14 요구, (b) KASLR이 RELOCATABLE 요구, (c) 컴파일타임 PREEMPT_NONE 불가(부팅 `preempt=none`으로 대체) → `presets/arch/riscv64-mainline.config` 변형으로 처리(98줄 통과).

## 5. ★ 핵심: 조기부팅 다단계 + KASLR 디버깅 (실검증 완료)

head.S 최초 명령(MMU off, 물리주소)부터 → MMU 켜지는 순간 → 물리→고VA 전이(애매한 상태) →
고VA 가동까지, **하나의 연속 세션에서** 단계별로 밟으며 심볼라이제이션이 오동작 없이 유지되는지,
**KASLR을 켜고도** 되는지 실제 부팅으로 검증했다. gdbtools의 arch/버전 무관 opcode 스캔이
핵심(하드코딩 주소 없음).

**검증 매트릭스 (실측):**

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
kbuildlab run v6.12-arm64                 # nokaslr(결정적)
kbuildlab run v6.12-arm64 --kaslr         # KASLR 활성
kbuildlab run v6.12-x86_64 --no-kvm       # x86 조기부팅은 TCG 권장(HW 브레이크 결정성)
#   같은 커널 여러 개: 기본 포트가 차면 자동으로 다음 포트 사용(안내 출력), --port N으로 지정도 가능

# 창2: gdb(pwndbg + gdbtools) 부착 — 기본적으로 head.S _text(커널 최초 명령)에 자동 정지
kbuildlab attach v6.12-arm64          # 연결 즉시 kearly bootbreak → _text (--no-bootbreak로 raw)
(gdb) kearly regimes walk    # MMU 정지점 전부 무장
(gdb) continue               # 반복 -> entry→mmuon→crossing→virtual→start 순서로 통과
(gdb) kearly where / kearly mmu / kpt / addr2sym $pc / mmview / kcensus ...
# KASLR: (gdb) b start_kernel ; continue   # 슬라이드 자동 보정 후 hit
```
`[TREE]` 인자는 트리 이름·경로뿐 아니라 **커널 소스 최상위 폴더 자체**(`.../kernel`, 이름 임의)도 받는다(내용 기반 인식).

**완전한 OS로 부팅 + 게스트 안에서 작업:**
```
kbl run v6.12-arm64 --run            # frozen 없이 곧장 완성된 OS로 부팅
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
**실검증:** headless nvim으로 v6.12-arm64 게스트에 DAP 부착 → 세션 초기화 성공(`DAP_INIT OK`).
호환성: 3개 arch gdb 모두 GDB 17.2+Python+DAP(`-i dap`) 지원 확인, nvim↔kbuildlab은 `qemu.conf`
심볼릭 링크로 머신 팩트 브리지.

**갱신·재색인(#12):** `./sync-all.sh` — 전 트리 소스 fetch+ff → 증분 재빌드(.config 유지) → ctags/cscope/gtags 재색인 한 번에. 특정 트리만: `./sync-all.sh <트리...>`.

## 7. 코드 탐색 (clangd off, #3 #7)

- 각 트리: **ctags + cscope + GNU GLOBAL(gtags)** 색인 생성(빌드한 arch만). clangd 대신 이 3중 인덱스.
- nvim: 연구 폴더 전체에서 **clangd 자동 비활성화** 규칙 등록(`~/.local/share/nvim/lsp_filter/rules.json`, `within=연구폴더`) → 오탐 없음.

## 8. 게스트 = 완전한 개발 OS (rootfs) — 3종 arch 실검증 완료

`kbl run <tree>`는 커널만 올리는 게 아니라 **로그인·네트워크·관측·편집·디버깅이 되는 완성된 OS**로 부팅된다.
공식 buildroot 2026.02.3에서 빌드(`build-rootfs.sh <arch>`, defconfig `rootfs/defconfig-*`).
rootfs는 arch별로 공유(같은 arch의 v6.12/upstream/v4.6 트리가 같은 cpio를 `INITRD=`로 참조).

**`kbl run` 기본값(옵션 없이) = 진정한 OS + 조기부팅 디버거 대기, 전부 오버라이드 가능:**

| 기본 | 설명 | 끄기/바꾸기 |
|---|---|---|
| rootfs 탑재 | tree.conf `INITRD=`, 로그인 가능 | `--no-initrd` / `--initrd PATH` |
| gdb 대기(frozen) | `-S`, head.S 첫 명령에서 정지 | `--run`(즉시 부팅) |
| KVM off (TCG) | 결정적 조기부팅 HW 브레이크 | `--kvm`(x86) |
| KASLR off | nokaslr | `--kaslr` |
| 네트워크 on | user/virtio, DHCP 10.0.2.15 | `--no-net` |
| SSH 포워딩 | host `-p 2222` → guest 22 | `--ssh-port N` |

**실검증 (boot-verify.py / ssh-verify.py, v6.12 각 arch를 그 rootfs로 부팅 → 전부 PASS):**

| 항목 | x86_64 | arm64 | riscv64 |
|---|---|---|---|
| root 로그인 (pw: `root`) | ✅ | ✅ | ✅ |
| 네트워크: eth0 DHCP, `ping 8.8.8.8`, DNS | ✅ | ✅ | ✅ (virtio-mmio NIC) |
| perf (커널버전 일치) / bpftool / strace / tcpdump | ✅ | ✅ | ✅ |
| ftrace / uftrace | ✅ | ✅ | ✅ / ❌(uftrace riscv 미지원) |
| ltrace | ✅ | ✅ | ❌ riscv 미지원 |
| vim 9.1 / bash 5.2 / python3 | ✅ | ✅ | ✅ |
| **gdb 15.2** (in-guest 유저스페이스 디버깅) | ✅ | ✅ | ✅ |
| **libbpf 1.6** (eBPF 런타임) | ✅ | ✅ | ✅ |
| **sshd (OpenSSH 10.2)** → nvim 원격 편집 | ✅ | ✅ | ✅ |

- **neovim 원격 편집:** `ssh -p 2222 root@localhost`(pw `root`) 또는 `nvim scp://root@localhost:2222//path`. 호스트→게스트 SSH 로그인 실증(`ssh-verify.py`, SSH_ASKPASS force). 호스트 공개키가 있으면 자동으로 authorized_keys 주입(무암호).
- **eBPF/C 개발(호스트 컴파일 방식):** eBPF는 호스트 `clang -target bpf`(BPF 바이트코드는 arch 무관)로 빌드 → 게스트에 주입 → `bpftool prog load`. 일반 C는 호스트 크로스 gcc로 빌드해 주입. 게스트엔 bpftool + libbpf + 커널 BTF(`/sys/kernel/btf/vmlinux`) 존재. **게스트 안 gcc/g++/clang 컴파일은 buildroot 구조상 불가**(호스트 크로스 전용); 단 타겟 libstdc++ 런타임이 있어 호스트-빌드 C++ 바이너리 실행·gdb 디버깅은 됨.
- **post-build(`rootfs/post-build.sh`, 3종 공통):** libsframe 복사(bpftool 의존), perf 복사(`rootfs/perf-<arch>`), eth0 DHCP stanza(증분빌드 대비), sshd root 로그인 허용, 호스트 pubkey 주입.
- **미포함:** drgn(buildroot 패키지 없음), 게스트 네이티브 gcc/clang/neovim(구조상 불가 → 호스트에서).

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

`logs/run-tests.sh` — 각 테스트가 자기 QEMU만 띄우고(고유 포트, `--smp 1`) 자기 PID만 정리, 동시 4개(~4코어)로 병렬. **37/37 PASS, 0 FAIL** (재실행 가능: `bash logs/run-tests.sh`, 결과 `logs/test-results.tsv`).

- **kaslr_fb_arm64 (6):** `_slide_via_pc_pa` — 물리 regime에서 None, virtual에서 값==kimage_voffset 참값, 40회 연타·garbage 주소에도 무크래시.
- **KASLR e2e:** arm64 on(shifted VA, slide≠0)/off(link VA, slide=0), x86 on(디컴프레서 베이스+슬라이드+hit)/off, upstream(mainline), v4.6(레거시), riscv — 총 11 assertion.
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
