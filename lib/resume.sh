#!/usr/bin/env bash
# Resume a guest a dead debugger left paused, through QMP.
#
# A gdb that is killed while it has the guest stopped releases the gdbstub at
# once -- qemu notices the closed socket -- but the guest stays paused, and
# nothing on the gdbstub side will ever start it again.  Measured on
# upstream-arm64: paused at +2s, +10s, +30s and +60s after SIGKILL, with the
# stub already free.  QMP is the other channel, and `cont` is the way back; a
# fresh gdb attaches normally afterwards.
set -uo pipefail
_self="$(readlink -f "${BASH_SOURCE[0]}")"
KBL_REPO="$(cd -P "$(dirname "$_self")/.." && pwd)"
# shellcheck source=/dev/null
source "${KBL_REPO}/lib/common.sh"

PORT=""; PORT_SET=0; LIST=0; FIRST=0; FORCE=0
declare -a REST=()
_usage() {
    cat <<USAGE
kbuildlab resume [TREE] [options]
  resume a guest that is paused with no debugger on it, over QMP

  --port|-p N   pick the guest by its GDB port
  --list|-l     list live guests and exit; never prompts
  --first       with several matches, take the lowest port instead of asking
  --force       resume even while a debugger is attached.  That debugger still
                believes it stopped the guest, and its next packet will disagree
                with reality -- detach it instead if you can

  TREE is a name, directory, or kernel source root; omitted, the current tree.
USAGE
}
while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help) _usage; exit 0 ;;
        --port|-p) PORT="${2:?--port needs a number}"; PORT_SET=1; shift 2 ;;
        --list|-l) LIST=1; shift ;;
        --first)   FIRST=1; shift ;;
        --force)   FORCE=1; shift ;;
        -*) die "resume: unknown option '$1'" ;;
        *)  REST+=("$1"); shift ;;
    esac
done

_want_tree=""
if [[ -n "${REST[0]:-}" ]]; then
    _want_tree="$(kbl_tree "${REST[0]}")" || exit 1
elif [[ $PORT_SET -eq 0 ]]; then
    _want_tree="$(kbl_tree "" 2>/dev/null)" || _want_tree=""
fi

kbl_pick_guest resume "$_want_tree" "$PORT" "$PORT_SET" "$FIRST" "$LIST"; _rc=$?
[[ $_rc -eq 2 ]] && exit 0
[[ $_rc -eq 0 ]] || exit $_rc

kbl_row_fields "$KBL_GUEST_ROW"
PORT="$_r_port"
[[ -n "$_r_sf" && -r "$_r_sf" ]] || die "resume: :$PORT has a live guest but no readable run
       state, so there is nothing that records where its QMP monitor is."
qmpport="$(kbl_state_get "$_r_sf" KBL_QMP_PORT)"
[[ -n "$qmpport" ]] || die "resume: the guest on :$PORT was started without a QMP monitor
       (--no-qmp), and the gdbstub cannot be asked to resume it from out here."

state="$(kbl_qmp_runstate "$_r_sf")"
dbg="$(kbl_gdb_attached "$PORT" "$_r_pid" "$(kbl_qemu_gdb_bind "$_r_pid")")"
say "guest        ${_r_name:-?} on :$PORT -- runstate ${state:-unknown}, debugger $dbg"

case "$state" in
    running) say "resume       nothing to do; it is already running"; exit 0 ;;
    prelaunch)
        die "resume: this guest has never been started -- it is frozen at its reset
       vector by 'run' itself, which is what --run or a debugger's first continue
       is for.  Resuming it here would skip the stop you asked for." ;;
esac

case "$dbg" in
    attached*)
        [[ $FORCE -eq 1 ]] || die "resume: :$PORT still has a debugger attached, so this guest is
       paused on purpose.  Resuming behind it leaves that debugger believing it
       holds a stopped guest.  Continue or detach there instead, or --force."
        warn "resume: resuming under an attached debugger (--force); it will disagree" ;;
esac

out="$(python3 - "$qmpport" <<'PY' 2>&1
import json, socket, sys
try:
    s = socket.create_connection(("127.0.0.1", int(sys.argv[1])), timeout=5)
    s.recv(65536); s.sendall(b'{"execute":"qmp_capabilities"}\n'); s.recv(65536)
    s.sendall(b'{"execute":"cont"}\n')
    for _ in range(4):
        line = s.recv(65536).splitlines()[0]
        msg = json.loads(line)
        if "error" in msg:
            print("ERROR", msg["error"].get("desc", msg["error"])); break
        if "return" in msg:
            print("OK"); break
except Exception as e:
    print("ERROR", e)
PY
)"
case "$out" in
    OK*) : ;;
    *)   die "resume: QMP refused: ${out#ERROR }" ;;
esac

state="$(kbl_qmp_runstate "$_r_sf")"
say "resume       runstate now ${state:-unknown}"
[[ "$state" == running ]] || warn "resume: it did not reach 'running'; something else is holding it"
