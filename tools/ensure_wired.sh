#!/bin/bash
# =============================================================================
# ensure_wired.sh — every wired interface keeps a connection that comes up
#                   by itself
# =============================================================================
#
# Ethernet is how a unit is reached when Wi-Fi is not: on the bench, for
# debugging, and by the farmer. Nothing in the OWL may leave eth0 without a
# working NetworkManager profile.
#
# Why this exists: until 2026-10-19 dev/clean.sh deleted every connection
# except the hotspot, "Wired connection 1" included. NetworkManager never
# recreates a deleted default wired profile (it records the interface in
# /var/lib/NetworkManager/no-auto-default.state), so a unit cleaned before
# that date, or cloned from an image that was, has had eth0 "disconnected"
# with no address ever since. This puts a profile back.
#
# USAGE
#   bash tools/ensure_wired.sh            repair (root, or the OWL user's
#                                         sudo nmcli grant from setup.sh)
#   bash tools/ensure_wired.sh --check    report only; exit 1 when a wired
#                                         interface has no usable profile
#
# For each ethernet device NetworkManager knows about:
#   - a wired profile that applies to it and autoconnects: left exactly as
#     it is (a static owl-ethernet / owl-controller-ethernet from setup, the
#     OS default, or one a person made);
#   - wired profiles that apply but none autoconnects: autoconnect is
#     switched on for them;
#   - no wired profile: 'owl-wired-<device>' is created (DHCP, autoconnect,
#     retried for ever).
# It never deletes, renames or changes addresses on anything.
#
# Exit: 0 fine (or no wired interface / no NetworkManager), 1 a wired
# interface has no usable profile (--check, or the repair failed).
# =============================================================================
set -uo pipefail

CHECK=0
[[ "${1:-}" == "--check" ]] && CHECK=1

say() { echo "[wired] $*"; }

if ! command -v nmcli >/dev/null 2>&1; then
    say "NetworkManager (nmcli) not found: nothing checked"
    exit 0
fi

# Reads need no privileges; changes do
if [[ $EUID -eq 0 ]]; then
    change() { nmcli "$@"; }
else
    change() { sudo -n nmcli "$@"; }
fi

devices="$(nmcli -t -f DEVICE,TYPE device 2>/dev/null | awk -F: '$2 == "ethernet" { print $1 }')"
if [[ -z "$devices" ]]; then
    say "no wired interface on this unit"
    exit 0
fi

status=0
for device in $devices; do
    usable=""        # a profile that applies and autoconnects
    dormant=()       # profiles that apply but do not autoconnect
    while IFS=: read -r uuid type; do
        [[ "$type" == "802-3-ethernet" ]] || continue
        bound="$(nmcli -g connection.interface-name connection show uuid "$uuid" 2>/dev/null)"
        [[ -z "$bound" || "$bound" == "$device" ]] || continue
        auto="$(nmcli -g connection.autoconnect connection show uuid "$uuid" 2>/dev/null)"
        if [[ "$auto" == "yes" ]]; then
            usable="$uuid"
        else
            dormant+=("$uuid")
        fi
    done < <(nmcli -t -f UUID,TYPE connection show 2>/dev/null)

    if [[ -n "$usable" ]]; then
        say "$device: wired profile present, connects by itself"
        continue
    fi

    if [[ "$CHECK" == "1" ]]; then
        if (( ${#dormant[@]} )); then
            say "$device: PROBLEM: its wired profile does not connect by itself"
        else
            say "$device: PROBLEM: no wired profile, the port will stay down"
        fi
        status=1
        continue
    fi

    if (( ${#dormant[@]} )); then
        for uuid in "${dormant[@]}"; do
            if change connection modify uuid "$uuid" connection.autoconnect yes; then
                say "$device: wired profile set to connect by itself"
            else
                say "$device: PROBLEM: could not switch autoconnect on"
                status=1
            fi
        done
    else
        name="owl-wired-$device"
        if change connection add type ethernet con-name "$name" ifname "$device" \
                connection.autoconnect yes connection.autoconnect-retries 0 \
                ipv4.method auto ipv6.method auto >/dev/null; then
            say "$device: no wired profile was left; created '$name' (DHCP, connects by itself)"
            # A 169.254 address as well as DHCP, so a laptop plugged straight
            # in can reach the unit. NetworkManager 1.40+; older ones refuse
            # the property and keep plain DHCP.
            change connection modify "$name" ipv4.link-local enabled >/dev/null 2>&1 || true
        else
            say "$device: PROBLEM: could not create a wired profile"
            status=1
            continue
        fi
    fi
    # Cable may be unplugged: not an error
    change device connect "$device" >/dev/null 2>&1 || true
done

exit $status
