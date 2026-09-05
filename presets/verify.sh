#!/usr/bin/env bash
# Read a built .config back and fail on anything the preset asked for that did
# not survive.
#
# This exists because the failure mode of a Kconfig fragment is silence.  A
# symbol whose `depends on` is unmet is not rejected -- it is dropped, and
# `make olddefconfig` writes a .config that simply does not mention it.  The
# build then succeeds and the feature is missing at runtime, which is where you
# find out.  Every line the preset states is checked here instead.
set -uo pipefail

CONFIG="${1:?usage: verify.sh <.config> <fragment> [fragment...]}"
shift
[[ -r "$CONFIG" ]] || { echo "verify: cannot read $CONFIG" >&2; exit 2; }

fail=0
checked=0
absent=0

state() {   # state SYMBOL -> the line as .config has it, or ""
    grep -E "^(CONFIG_$1=|# CONFIG_$1 is not set)" "$CONFIG" | head -1
}

# Does this kernel DEFINE the symbol at all?
#
# A preset line that names a symbol the tree has never heard of is not drift: it
# is a preset written against a newer kernel being read back against an older
# one.  v4.6 has no DEBUG_INFO_DWARF5 (5.2), no DEBUG_INFO_BTF (5.2) and no
# DEBUG_INFO_COMPRESSED (5.14), so asking for them there can only ever fail --
# and reporting that as a regression trains the reader to ignore this output,
# which is the one thing a check like this cannot afford.
#
# Answered from the tree's own Kconfig files, so it stays true for whatever
# kernel is in front of it; asked only for symbols that are already missing, so
# the cost is a handful of greps rather than one per preset line.
SRC="$(cd -P "$(dirname "$CONFIG")" && pwd)"
symbol_exists() {
    [ -d "$SRC/arch" ] || return 0        # not a kernel tree: cannot tell, assume it does
    grep -rlqE "^[[:space:]]*(menu)?config[[:space:]]+$1([[:space:]]|\$)" \
        --include='Kconfig*' "$SRC" 2>/dev/null
}

for frag in "$@"; do
    [[ -r "$frag" ]] || { echo "verify: cannot read $frag" >&2; exit 2; }
    while IFS= read -r line; do
        case "$line" in
            CONFIG_*=*)
                sym="${line%%=*}"; sym="${sym#CONFIG_}"; want="${line#*=}"
                checked=$((checked + 1))
                got="$(state "$sym")"
                if [[ -z "$got" ]]; then
                    if symbol_exists "$sym"; then
                        printf 'MISSING  CONFIG_%s=%s\n         asked for by %s, absent from the .config although this kernel\n         defines the symbol -- a `depends on` was not met, or another fragment overrode it\n' \
                            "$sym" "$want" "$(basename "$frag")"
                        fail=$((fail + 1))
                    else
                        printf 'ABSENT   CONFIG_%s=%s\n         %s asks for it; this kernel does not define the symbol at all,\n         so it is a preset written for a newer version, not a setting that was lost\n' \
                            "$sym" "$want" "$(basename "$frag")"
                        absent=$((absent + 1))
                    fi
                elif [[ "$got" != "CONFIG_$sym=$want" ]]; then
                    printf 'CHANGED  CONFIG_%s: asked %s, got %s  (%s)\n' \
                        "$sym" "$want" "${got#*=}" "$(basename "$frag")"
                    fail=$((fail + 1))
                fi
                ;;
            "# CONFIG_"*" is not set")
                sym="${line#\# CONFIG_}"; sym="${sym% is not set}"
                checked=$((checked + 1))
                got="$(state "$sym")"
                if [[ -n "$got" && "$got" != "# CONFIG_$sym is not set" ]]; then
                    printf 'SET      CONFIG_%s: asked off, got %s  (%s)\n' \
                        "$sym" "${got#*=}" "$(basename "$frag")"
                    fail=$((fail + 1))
                fi
                # absent is fine for a negative: the symbol may not exist on
                # this architecture at all, which is the same outcome as off
                ;;
        esac
    done < "$frag"
done

if [[ $fail -gt 0 ]]; then
    printf '\nverify: %d of %d preset lines did not survive%s\n' "$fail" "$checked" \
        "$([[ $absent -gt 0 ]] && printf ' (%d more name symbols this kernel does not have)' "$absent")" >&2
    exit 1
fi
if [[ $absent -gt 0 ]]; then
    printf 'verify: %d of %d preset lines survived; %d name symbols this kernel version does not define\n' \
        "$((checked - absent))" "$checked" "$absent"
else
    printf 'verify: all %d preset lines survived\n' "$checked"
fi
