# 펌웨어/부트/디버깅 재구성 — 지시·설계·상태 (지시 유실 방지용, 매 작업 전 재읽기)

## 0. 불변 원칙 (사용자 지시)
- **노-하드코딩**: 독립 깃 프로젝트(kbuildlab/gdbtools/u-boot)엔 연구별 값 절대 주입 금지. 하드코딩은 내 로컬 스크립트(이 폴더)·tree.conf·CLI 인자에만. 인자 입력은 LLM(내)이 수행.
- **정리**: 내가 띄운 QEMU/docker만 PID로 표적 정리. 일괄 kill 금지. 정리 후 count==0 확인. 100% 됐다고 막 죽이지 말 것.
- **자원**: CPU<90%, 코어 4~6개.
- **공급망**: 클론은 공식 사이트·HTTPS·무결성 검증(커밋 대조). u-boot v2026.07=denx ece349ad, github미러 일치 확인 완료.
- **커밋/푸시**: **"모든 작업이 진짜 다 끝나면"** kbuildlab·gdbtools 커밋+푸시(사용자 승인 완료, 2026-08-29). 각 repo **기존 커밋 메시지 규약을 git log로 정독 후 따를 것**. AI-attribution 트레일러는 repo 이력에 이미 있을 때만. u-boot는 순정 클론(소스 미수정)이라 커밋 없음. **완료 전까지는 절대 커밋 금지**(현재 미완).
- **재귀 테스트 + 점진 개선 상시**: 매 변경마다 회귀·엣지 재검증, 오탐 정정, 종단 도달까지 반복.
- **레드팀 무한**: happy-path 금지, 비정상 경로까지 전부. **false positive(오탐)가 최악** — "성공" 판정은 실제 심볼/PC/값으로 검증.
- **기본 다지고 고도화**: 토대 먼저, 그 위에 정교화.
- **커밋 대상(kbuildlab/gdbtools) 한글 금지**: 코드·주석·md 영어만(내 추가분 한정). 기존 원저자 한글(session.py:655, docs/early-boot.md 587줄)은 사용자 콘텐츠 → 임의 개변 금지, 요청 시만 번역.
- **완료 후 고도화 리팩터**(END): 책임 분리, 외부 인자 주입(DI), 역할·레이어 분담으로 하드코딩 최소화. run(부팅조합)·attach(정지조합)·gdbtools(심볼/regime)·연구스크립트(경로/디스크) 레이어 경계 정리.

## 1. 부트 체인 (아키텍처별)
| arch | 기본(임베디드/서버) | UEFI 옵션(비기본) | direct(빠른 -kernel, 유지) |
|---|---|---|---|
| x86_64 | UEFI(OVMF)→grub2 [서버] | (기본이 UEFI) | `--boot direct` |
| arm64 | u-boot→커널 [임베디드] | edk2 AAVMF→grub2 | `--boot direct` |
| riscv64 | OpenSBI→u-boot→커널 [임베디드] | OpenSBI→(u-boot/edk2)UEFI→grub2 | `--boot direct`(=OpenSBI→커널) |

## 2. 디버깅 기본값 (부트모드 무관, 전 7종)
- **기본 stop = head.S `_text`** (아무 옵션 없이). 펌웨어 모드면 **커널 로드/복구주소에 HW-bp**로 도달.
- 임의 단계 attach 가능: 펌웨어 `_start`(빅뱅, PC=0/0x1000) ~ 커널. 옵션으로 정지지점 선택.
- **gdbtools 전단계 심볼·context display**: u-boot 구간=u-boot ELF, 커널 구간=vmlinux 자동 전환. 심볼 없는 구간은 raw 디스어셈블.
- **nvim 어댑터 기본값도 `_text`**.
- 구조적으로 불가능한 것 외 **모든 gdb 본래 기능 동작**. 한계는 문서화.
- **멀티 인스턴스 조합 매칭**: 이름/경로 같아도 (트리·부트모드·로드주소·포트) 조합 복수 가능 → run이 per-port 상태 기록, attach가 그 조합 매칭.

## 3. 페이징 프로파일 (2026 현업 최다)
- x86: 4KB/4레벨(48bit). **런타임**(QEMU `-cpu ...,-la57`), 재빌드 불필요.
- arm64: 4KB/39bit/3레벨. **컴파일타임** → v6.12·upstream **재빌드 필요**(v4.6는 이미 39/3, 불필요).
- riscv: 4KB/Sv39. **런타임**(QEMU `-cpu ...,sv39=on,sv48=off,sv57=off`), 재빌드 불필요.
- arm64 재빌드는 sync 선행. tree config-fragment(로컬)에 `CONFIG_ARM64_VA_BITS_39=y`.

## 4. rootfs/디스크
- 게스트=완전한 OS(로그인/네트워크/perf·strace·tcpdump·bpftool·vim·bash·gdb·libbpf·sshd). 검증 완료(3 arch).
- eBPF/C는 호스트 clang(BPF arch무관)/크로스 빌드→주입(게스트 gcc 불가=buildroot 구조).
- **7종 트리별 독립 영속 디스크**(재부팅 후 apt/코드 유지) — 고도화 항목. 391G 여유라 OK.
- 부트디스크: SSD 연구폴더에 1회 pre-build(`firmware/build-bootdisk.sh`) → run이 /dev/shm(RAM) 복사 + boot.scr에 bootargs(kaslr/console) 주입. 파티션 장난 매번 금지.

## 5. Red-team 검증 완료 (실측)
- riscv QEMU gdbstub HW-bp 작동(게스트 HW watchpoint 부재와 무관).
- arm64 u-boot+KASLR: HW-bp@로드주소 → _text(자기재배치 前 진입점).
- x86: 디컴프레서 복구+HW-bp → startup_64. **TCG·KVM 둘 다** 실측 성공.
- 펌웨어 `_start`(PC=0) attach + u-boot ELF 심볼 + SCTLR 등 레지스터 read 성공.
- **결함 발견·수정 확정**: 펌웨어 모드에서 QEMU `-append` 미전달 → bootargs는 boot.scr에 주입.

## 6b. 확정 실행 순서 (자율 진행 — 매번 "다음 뭐?" 묻지 말 것)
1. **arm64 완성** ✅ (3트리 uboot 기본, _text 검증)
2. **Phase B**: gdbtools 전단계 심볼(u-boot ELF add-symbol-file, 펌웨어 구간) + raw pre-MMU context 예외 가드 + `--stop text|firmware|start_kernel` 정지지점 옵션.
3. **Phase C**: (a) arm64 v6.12/upstream VA_BITS_39 재빌드(sync 선행) (b) run-qemu 아키텍처별 페이징 CPU 플래그(x86 `-la57` off, riscv sv39) (c) 트리별 영속 writable 디스크.
4. **Phase D**: riscv(OpenSBI→u-boot, 임베디드) → x86(OVMF+grub2, 서버). 각 펌웨어 출처검증(공식·HTTPS·해시).
5. **Phase E**: nvim 어댑터 기본값(_text) + **7종을 기본옵션(head.S 최초 attach)으로 gdb·nvim 양쪽 attach → 기본명령(b/bp/c/si/ni/s/n/disas/p/x)+breakpoint·watchpoint를 **거는 시점·방식별로** 다양한 케이스 심층검증(구조적 불가능한 경우만 예외로 문서화, false-positive 회피)** + 빅뱅·도구 전수검증.
6. **고도화 리팩터**: 책임분리/DI/레이어(§0).
7. **커밋+푸시**: kbuildlab·gdbtools(각 repo git log 규약 정독 후, 한글·AI트레일러 금지).
- 각 단계에 **무한 red-team**(비정상경로·오탐경계) + 재귀검증. 진짜 블로커/결정만 물음.

## 6. 진행 상태 / 로드맵
- [x] u-boot 공식 빌드(v2026.07) + 체인 부팅(로그인) + HW-bp bootbreak 실증(arm64).
- [x] **Phase A 완료(검증)**: run-qemu `--boot direct|uboot`(tree.conf `UBOOT/UBOOT_LOADADDR/BOOTDISK`), `firmware/build-bootdisk.sh`(SSD pre-build), per-port 상태파일(`/dev/shm/kbl-run-<port>.env`), RAM 디스크 격리+trap정리, boot.scr bootargs 주입. run-gdb가 상태파일→`GDBTOOLS_ENTRY_PA`+`BREAK_KIND=hw`. **`kbl run v6.12-arm64 --boot uboot`+`kbl attach`만으로 u-boot→head.S _text(0x40200000, hw-bp) 정확 정지 e2e 검증.** direct 회귀 clean(sw-bp vs hw-bp로 경로 분리 확증). gdbtools 코드 변경 0(기존 ENTRY_PA/BREAK_KIND/preset 활용). **red-team: 내 회귀 테스트가 v4.6주소(0x40080000)를 v6.12에 잘못 기대한 오탐 → break_kind 증거로 정정.**
- [ ] **Phase A 잔여**: arm64 3트리 부트디스크 + tree.conf BOOT=uboot 기본화(전 트리 검증 후). direct 상태파일 누출(exec라 trap불가) 개선.
- [ ] **Phase B 잔여**: gdbtools 전단계 심볼(u-boot ELF@_start↔vmlinux) + raw context 예외 가드 + 정지지점 옵션(firmware/_start/start_kernel).
- [x] **Phase C 완료(검증)**: 페이징 x86 4레벨(la57=off)·arm64 39bit(재빌드)·riscv Sv39(satp MODE=8). 디버깅 전 regime 무손상. run-qemu CPU_PAGING(tree.conf).
- [x] **Phase D 완료(검증)**: x86 UEFI(OVMF→grub2→커널→로그인 + startup_64 hw-bp) BOOT=uefi 기본화. riscv OpenSBI→u-boot 부팅OK but 0x80200000충돌→기본 direct(OpenSBI→커널, _start clean). build-esp.sh(ESP+grub2). run-qemu uefi분기(OVMF pflash+VARS RAM복사+grub.cfg per-run). **미결정교화: riscv uboot magic-skip, x86 UEFI 완전동적 디컴프레서복구(현재 UEFI_ENTRY=이미지베이스 정적, 재빌드로 EFI 로드주소 바뀌면 재확인).**
- [x] **x86 UEFI ELF-엔트리 변위 버그 수정(2026-08-29, 근본원인 규명)**: upstream-x86_64 UEFI 기본조합이 hw-bp 미발화로 hang. red-team 심층진단으로 규명:
  - 오진 경로 배제: (1) TCG hw-bp miss 가설 → KVM도 동일 미발화로 반증 (2) 물리배치 비결정성 가설 → 3회 반복 phys_base=0x800000 결정적으로 반증.
  - **진짜 원인**: `UEFI_ENTRY`를 `_text` PA(/proc/iomem "Kernel code" 시작=0x1800000, =이미지 베이스)로 설정했으나, x86_64 **ELF 엔트리(startup_64)는 _text와 분리될 수 있음**. upstream은 startup_64가 `.init.text`(link VA 0xffffffff8396bb70, PA 0x416bb70), v6.12는 startup_64==_text(우연히 일치). 디컴프레서는 ELF 엔트리로 점프 → _text PA에 건 hw-bp는 영원히 미발화. 실증: hw-bp@0x416bb70 발화(`mov r15,rsi`=startup_64 첫 명령).
  - **수정(gdbtools, 공유 레이어)**: `session.py` bootbreak가 x86에서 이미지베이스(ENTRY_PA)에 `symval(entry_symbol)-symval(_text)` 오프셋을 더해 진짜 ELF 엔트리에서 정지. README 정의(ENTRY_PA=이미지 베이스)와 일치. direct 동적복구는 이미 ELF 엔트리 PA를 반환하므로 `_entry_recovered` 플래그로 이중적용 차단. arm64/riscv는 x86 전용 가드로 no-op. **터미널·nvim 양 경로가 단일 수정으로 해결**.
  - **레이어 정리**: run-gdb는 이미지베이스를 그대로 전달(상태파일 KBL_LOADADDR), tree.conf의 `GDBTOOLS_ENTRY_PA`는 bulk-read에서 제외(per-port 상태파일이 mode-aware하게 우선 — direct 포트는 여전히 decompressor recovery). 7종 tree.conf에 `GDBTOOLS_ENTRY_PA`(이미지베이스/로드주소) 명시 → nvim(qemu.conf만 읽음)이 동일 경로 사용. arm64 3종은 `GDBTOOLS_BREAK_KIND=hw`도 명시(u-boot 체인 엔트리 덮어씀 대비, 터미널과 동일 robustness; arch 기본 sw는 direct 전제).
- [x] **Phase E 검증(2026-08-29)**: 터미널 7종 전수 — head.S 최초 지점에서 핵심 명령(entry/x/p/si/disas/break+continue/bt/watch) ALL PASS(0 FAIL), 오탐가드(raw mtime 계단식·실심볼·섹션). nvim 등가 경로(qemu.conf의 GDBTOOLS_* 주입 후 kearly bootbreak) 7종 head.S 엔트리 착지 확인: x86=startup_64(hw,시프트), arm64=_text(hw), riscv=_start(sw). pwndbg 컨텍스트(레지스터/EARLY-BOOT regime/disasm) 정상 표시.
  - **break/watch "거는 시점·방식별" 매트릭스 7종 ALL PASS**(logs/bwmatrix.sh): si·ni·next·step 스텝, **물리(MMU off) regime에서 심볼로 건 브레이크가 크로싱 후 high-VA에서 발화**(bSym@phys), **watch jiffies 실발화**(Old/New value 실측, do_timer). 오탐가드: 실 Old/New 값·실심볼 요구. 핵심 = "거는 방식에 따라 작동/미작동"하는 크로스-regime 케이스가 전부 실동작.
- [x] **Phase E 완료**: nvim 기본값(_text/시프트), 7종 전수(터미널+nvim+break/watch 매트릭스). 위 항목 참조.
- [x] **트리별 영속 writable 디스크 완료(2026-08-29, 고도화)**: `kbuildlab run --persist`(tree.conf `PERSIST=1`/`PERSIST_SIZE`/`PERSIST_DISK`, 기본 on). 호스트가 `<tree>/disk/persist.img`(SSD) 생성+mkfs.ext4(label kblpersist)+virtio-blk 부착(serial=kblpersist, x86/arm64=pci·riscv=mmio). 게스트 `rootfs/post-build.sh`의 `/etc/init.d/S45persist`가 serial로 디스크 식별→mount(우선)·실패시 포맷→`/persist`. 부트모드 무관(direct/uboot/uefi). 연결부 검증: rootfs cpio 재packing(fakeroot, device node 보존)→부트디스크(esp/uboot) 재빌드. **에러핸들링**: 멀티인스턴스 flock 대신 /proc 스캔 lock(동시쓰기시 2번째는 persist 없이 부팅), 생성/포맷 실패 non-fatal, `--no-persist` 우회. **실증**: x86 direct 2-phase 재부팅 영속(debugfs로 marker 확인), 3부트모드×3arch mount+write, 조기부팅 7/7 무회귀. **red-team 결함 2건 수정**: (1) busybox blkid has-fs 오판→mount-우선 방식 (2) _persist_busy `-x` 전체줄매칭이 `if=none,` 접두로 실패→부분일치.
- 무한 red-team은 전 단계에 상시 적용.
