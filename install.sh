#!/usr/bin/env bash
# Put the `kbuildlab` command on PATH and record this checkout, so a program
# started without that PATH (an editor launched from a desktop menu) still
# finds it.  The command is a symlink into this checkout, so a `git pull` here
# updates it.
#
#   ./install.sh                  ~/.local/bin (XDG_BIN_HOME), for this user
#   ./install.sh --system         /usr/local/bin, for every user who can read
#                                 this checkout; run it as yourself and it asks
#                                 sudo for the links only, then records as you
#   ./install.sh --prefix DIR     DIR/bin (through sudo too when you cannot write it)
#   ./install.sh --sudo CMD       what asks for root: sudo, else doas, else run0
#   ./install.sh --alias NAME     also install it as NAME (repeatable), e.g. kbl
#   ./install.sh --record         only record this checkout (after `sudo ./install.sh --system`)
#   ./install.sh --check | --uninstall
#
# Both default directories are on PATH on Debian, Ubuntu, Fedora, RHEL and
# openSUSE once they exist; on Arch and Void ~/.local/bin is added by the
# user's shell setup, and this says how when it is missing.  Shell files are
# never edited, and nothing is installed from a package manager: `kbuildlab
# doctor` names what is missing and the package that provides it.
set -euo pipefail
REPO="$(cd -P "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
NAME=kbuildlab
MODE=install; WHERE=user; PREFIX=""; SUDO_CMD=""
declare -a ALIASES=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --user) WHERE=user; shift ;;
        --system) WHERE=system; shift ;;
        --prefix) WHERE=prefix; PREFIX="${2:?--prefix needs a directory}"; shift 2 ;;
        --alias) ALIASES+=("${2:?--alias needs a name}"); shift 2 ;;
        --sudo) SUDO_CMD="${2:?--sudo needs a command}"; shift 2 ;;
        --record) MODE=record; shift ;;
        --check) MODE=check; shift ;;
        --uninstall) MODE=uninstall; shift ;;
        -h|--help) sed -n '2,/^[^#]/{/^#/s/^# \{0,1\}//p}' "$0"; exit 0 ;;
        *) echo "install.sh: unknown option '$1'" >&2; exit 2 ;;
    esac
done

if [[ -t 1 && -z "${NO_COLOR:-}" && "${TERM:-dumb}" != dumb ]]; then
    G=$'\e[32m' R=$'\e[31m' C=$'\e[36m' D=$'\e[2m' N=$'\e[0m'
else G="" R="" C="" D="" N=""; fi
case "${LC_ALL:-${LC_CTYPE:-${LANG:-}}}" in
    *[Uu][Tt][Ff]-8*|*[Uu][Tt][Ff]8*) OKM="✓" NOM="✗" ;; *) OKM="+" NOM="x" ;;
esac
ok()  { printf '  %s%s%s %s\n' "$G" "$OKM" "$N" "$*"; }
bad() { printf '  %s%s%s %s\n' "$R" "$NOM" "$N" "$*"; }
say() { printf '  %s%-12s%s %s\n' "$C" "$1" "$N" "${*:2}"; }

case "$WHERE" in
    user)   BIN="${XDG_BIN_HOME:-$HOME/.local/bin}" ;;
    system) BIN=/usr/local/bin ;;
    prefix) BIN="${PREFIX%/}/bin" ;;
esac
TARGET="$REPO/bin/$NAME"
POINTER="${XDG_CONFIG_HOME:-$HOME/.config}/kbuildlab/root"

on_path() { case ":$PATH:" in *":$1:"*) return 0 ;; esac; return 1; }
path_hint() {
    on_path "$BIN" && return 0
    bad "$BIN is not on PATH; add it in your shell's startup file:"
    printf '      bash, zsh:  export PATH="%s:$PATH"\n' "$BIN"
    printf '      fish:       fish_add_path %s\n' "$BIN"
}
# Root only links into /usr/local/bin: what else this does belongs to the user
# who runs kbuildlab (their config), and under sudo it would land in root's.
if [[ "$(id -u)" -eq 0 && "$MODE" != check ]] && ! [[ "$WHERE" == system && "$MODE" != record ]]; then
    bad "run this as yourself; only --system (linking into /usr/local/bin) needs root"; exit 1
fi
user_scope() { [[ "$(id -u)" -ne 0 ]]; }

mine() { [[ -L "$1" && "$(readlink -f "$1")" == "$TARGET" ]]; }
dangling() { [[ -L "$1" && ! -e "$1" ]]; }

record() { mkdir -p "$(dirname "$POINTER")" && printf '%s\n' "$REPO" > "$POINTER" && ok "recorded in $POINTER"; }

# A link into a directory this user cannot write (--system, or a root-owned
# --prefix) goes through sudo, asked once before anything changes; the record
# stays this user's.  Never for ~/.local/bin: a root-owned link there is wrong.
declare -a ELEVATE=()
ask_root() {
    local d="$BIN" cmd
    [[ "$(id -u)" -ne 0 && "$WHERE" != user ]] || return 0
    while [[ ! -d "$d" ]]; do d="$(dirname "$d")"; done
    [[ -w "$d" ]] && return 0
    if [[ -n "$SUDO_CMD" ]]; then
        read -ra ELEVATE <<<"$SUDO_CMD"
    else
        for cmd in sudo doas run0; do command -v "$cmd" >/dev/null 2>&1 && { ELEVATE=("$cmd"); break; }; done
    fi
    if [[ ${#ELEVATE[@]} -eq 0 ]]; then
        bad "cannot write $d, and none of sudo, doas, run0 is installed; --sudo CMD names another"; exit 1
    fi
    command -v "${ELEVATE[0]}" >/dev/null 2>&1 || { bad "--sudo: ${ELEVATE[0]} is not installed"; exit 1; }
    say root "cannot write $d; asking ${ELEVATE[0]##*/} for the links only"
    if [[ "${ELEVATE[0]}" == sudo ]]; then
        "${ELEVATE[@]}" -v || { bad "sudo did not authenticate; nothing was changed"; exit 1; }
    fi
}
as_root() { "${ELEVATE[@]+"${ELEVATE[@]}"}" "$@"; }

# The first directory on the way to this checkout that other users cannot enter.
reach_block() {
    local d="$REPO/bin"
    while [[ "$d" != / && -n "$d" ]]; do
        [[ "$(stat -c %A "$d" 2>/dev/null)" == *[xt] ]] || { printf '%s\n' "$d"; return 0; }
        d="$(dirname "$d")"
    done
    return 1
}
reach_warn() {
    local blk
    [[ "$WHERE" != user ]] && blk="$(reach_block)" || return 0
    bad "other users cannot enter $blk ($(stat -c %A "$blk")), so the link works only for $(stat -c %U "$REPO")"
}

case "$MODE" in
    record) record ;;
    check)
        for n in "$NAME" "${ALIASES[@]+"${ALIASES[@]}"}"; do
            if mine "$BIN/$n"; then ok "$BIN/$n -> $TARGET"; else bad "$BIN/$n is not this checkout's $NAME"; fi
        done
        reach_warn
        if [[ -r "$POINTER" && "$(<"$POINTER")" == "$REPO" ]]; then ok "recorded in $POINTER"
        else bad "not recorded in $POINTER"; fi
        path_hint ;;
    uninstall)
        declare -a links=()
        for n in "$NAME" "${ALIASES[@]+"${ALIASES[@]}"}"; do
            if mine "$BIN/$n"; then links+=("$BIN/$n")
            else say kept "$BIN/$n (not a link to this checkout)"; fi
        done
        if [[ ${#links[@]} -gt 0 ]]; then
            ask_root
            as_root rm -f -- "${links[@]}" || { bad "could not remove the links from $BIN"; exit 1; }
            for l in "${links[@]}"; do ok "removed $l"; done
        fi
        if user_scope && [[ -r "$POINTER" && "$(<"$POINTER")" == "$REPO" ]]; then
            rm -f "$POINTER" && ok "removed $POINTER"
        fi ;;
    install)
        declare -a links=()
        for n in "$NAME" "${ALIASES[@]+"${ALIASES[@]}"}"; do
            if mine "$BIN/$n"; then ok "$BIN/$n -> $TARGET"; continue; fi
            if [[ -e "$BIN/$n" && ! -L "$BIN/$n" ]]; then bad "$BIN/$n exists and is not a symlink; skipped"; continue; fi
            if [[ -L "$BIN/$n" ]] && ! dangling "$BIN/$n"; then
                bad "$BIN/$n points at $(readlink -f "$BIN/$n"), another checkout; skipped"; continue
            fi
            dangling "$BIN/$n" && say replaced "$BIN/$n (it pointed at $(readlink "$BIN/$n"), which is gone)"
            links+=("$BIN/$n")
        done
        if [[ ${#links[@]} -gt 0 ]]; then
            ask_root
            # shellcheck disable=SC2016
            as_root sh -c 't="$1"; shift; mkdir -p "$(dirname "$1")" || exit; for l; do ln -sfn "$t" "$l" || exit; done' \
                sh "$TARGET" "${links[@]}" || { bad "could not link into $BIN"; exit 1; }
            for l in "${links[@]}"; do ok "$l -> $TARGET"; done
        fi
        reach_warn
        if user_scope; then
            record
            path_hint
            printf '  %snext: kbuildlab doctor%s\n' "$D" "$N"
        else
            say next "as yourself: $REPO/install.sh --record, then kbuildlab doctor"
        fi ;;
esac
