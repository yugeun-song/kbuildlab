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

state() {   # state SYMBOL -> the line as .config has it, or ""
    grep -E "^(CONFIG_$1=|# CONFIG_$1 is not set)" "$CONFIG" | head -1
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
                    printf 'MISSING  CONFIG_%s=%s\n         asked for by %s, absent from the .config --\n         a `depends on` was not met, or another fragment overrode it\n' \
                        "$sym" "$want" "$(basename "$frag")"
                    fail=$((fail + 1))
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
    printf '\nverify: %d of %d preset lines did not survive\n' "$fail" "$checked" >&2
    exit 1
fi
printf 'verify: all %d preset lines survived\n' "$checked"
