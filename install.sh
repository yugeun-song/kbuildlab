#!/usr/bin/env bash
# Put the `kbuildlab` command on PATH and record this checkout, so a program
# started without that PATH (an editor launched from a desktop menu) still
# finds it.  The command is a symlink into this checkout, so a `git pull` here
# updates it.
#
#   ./install.sh                  ~/.local/bin (XDG_BIN_HOME), for this user
#   ./install.sh --system         /usr/local/bin, for every user who can read
#                                 this checkout (run with sudo; it only links)
#   ./install.sh --prefix DIR     DIR/bin
#   ./install.sh --alias NAME     also install it as NAME (repeatable), e.g. kbl
#   ./install.sh --record         only record this checkout (after a --system install)
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
MODE=install; WHERE=user; PREFIX=""
declare -a ALIASES=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --user) WHERE=user; shift ;;
        --system) WHERE=system; shift ;;
        --prefix) WHERE=prefix; PREFIX="${2:?--prefix needs a directory}"; shift 2 ;;
        --alias) ALIASES+=("${2:?--alias needs a name}"); shift 2 ;;
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

case "$MODE" in
    record) record ;;
    check)
        for n in "$NAME" "${ALIASES[@]+"${ALIASES[@]}"}"; do
            if mine "$BIN/$n"; then ok "$BIN/$n -> $TARGET"; else bad "$BIN/$n is not this checkout's $NAME"; fi
        done
        if [[ -r "$POINTER" && "$(<"$POINTER")" == "$REPO" ]]; then ok "recorded in $POINTER"
        else bad "not recorded in $POINTER"; fi
        path_hint ;;
    uninstall)
        for n in "$NAME" "${ALIASES[@]+"${ALIASES[@]}"}"; do
            if mine "$BIN/$n"; then rm -f "$BIN/$n" && ok "removed $BIN/$n"
            else say kept "$BIN/$n (not a link to this checkout)"; fi
        done
        if user_scope && [[ -r "$POINTER" && "$(<"$POINTER")" == "$REPO" ]]; then
            rm -f "$POINTER" && ok "removed $POINTER"
        fi ;;
    install)
        if [[ ! -d "$BIN" ]]; then
            mkdir -p "$BIN" 2>/dev/null || { bad "cannot create $BIN$([[ $WHERE == system ]] && echo ' -- run with sudo')"; exit 1; }
        fi
        [[ -w "$BIN" ]] || { bad "$BIN is not writable$([[ $WHERE == system ]] && echo ' -- run with sudo')"; exit 1; }
        for n in "$NAME" "${ALIASES[@]+"${ALIASES[@]}"}"; do
            if [[ -e "$BIN/$n" && ! -L "$BIN/$n" ]]; then bad "$BIN/$n exists and is not a symlink; skipped"; continue; fi
            if [[ -L "$BIN/$n" ]] && ! mine "$BIN/$n" && ! dangling "$BIN/$n"; then
                bad "$BIN/$n points at $(readlink -f "$BIN/$n"), another checkout; skipped"; continue
            fi
            dangling "$BIN/$n" && say replaced "$BIN/$n (it pointed at $(readlink "$BIN/$n"), which is gone)"
            ln -sfn "$TARGET" "$BIN/$n" && ok "$BIN/$n -> $TARGET"
        done
        if user_scope; then record
        else say next "as yourself, not root: $REPO/install.sh --record"; fi
        path_hint
        printf '  %snext: kbuildlab doctor%s\n' "$D" "$N" ;;
esac
