#!/usr/bin/env bash
# Philips Sonicare BLE Pairing Script
# Scans for Philips Sonicare toothbrushes, pairs and trusts them for
# Home Assistant.
#
# Usage: ./scripts/pair.sh [MAC_ADDRESS]
#   If no MAC is given, scans for nearby Sonicare toothbrushes and lets
#   you choose.

set -euo pipefail

SCAN_SECONDS=20
# Classic (477ea600…) and newer (e50ba3c0…) Sonicare service UUID prefixes
SONICARE_UUID_PATTERN="477ea600\|e50ba3c0"

# --- colors ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

info()  { echo -e "${CYAN}[INFO]${NC}  $*" >&2; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*" >&2; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*" >&2; }
err()   { echo -e "${RED}[ERROR]${NC} $*" >&2; }

# --- pre-checks ---
if ! command -v bluetoothctl &>/dev/null; then
    err "bluetoothctl not found. Install bluez or run this on your Home Assistant host."
    exit 1
fi

# Check if bluetooth is powered on
if ! bluetoothctl show 2>/dev/null | grep -q "Powered: yes"; then
    warn "Bluetooth adapter is not powered on. Attempting to power on..."
    bluetoothctl power on 2>/dev/null || true
    sleep 1
    if ! bluetoothctl show 2>/dev/null | grep -q "Powered: yes"; then
        err "Could not power on Bluetooth adapter."
        exit 1
    fi
    ok "Bluetooth adapter powered on."
fi

# --- functions ---

find_sonicare_devices() {
    # Returns lines of: MAC<TAB>NAME  (stdout only)
    bluetoothctl devices 2>/dev/null | while read -r _ mac name; do
        if bluetoothctl info "$mac" 2>/dev/null | grep -qi "$SONICARE_UUID_PATTERN"; then
            echo -e "${mac}\t${name}"
        fi
    done
}

scan_for_devices() {
    info "Scanning for Sonicare toothbrushes (${SCAN_SECONDS}s)..."
    echo "    Press your toothbrush's power button NOW — it only stays" >&2
    echo "    awake for ~20 seconds and is NOT reachable while sitting" >&2
    echo "    on the charger. Press the button again if the scan is" >&2
    echo "    still running." >&2
    echo "" >&2

    # Scan — all output to stderr (display only)
    # No --line-buffered: BusyBox grep (HA Terminal add-on) doesn't support it
    bluetoothctl --timeout "$SCAN_SECONDS" scan on 2>/dev/null | \
        grep -i "philips\|sonicare" | \
        sed 's/^/    /' >&2 &
    local scan_pid=$!
    wait "$scan_pid" 2>/dev/null || true
    echo "" >&2
}

is_paired() {
    bluetoothctl info "$1" 2>/dev/null | grep -q "Paired: yes"
}

is_trusted() {
    bluetoothctl info "$1" 2>/dev/null | grep -q "Trusted: yes"
}

unpair_device() {
    local mac=$1
    info "Removing existing pairing for $mac..."
    bluetoothctl remove "$mac" 2>/dev/null || true
    sleep 2
    # Rescan to rediscover the device
    bluetoothctl --timeout 5 scan on 2>/dev/null > /dev/null || true
    ok "Previous pairing removed."
}

pair_device() {
    local mac=$1
    local name=$2

    echo "" >&2
    echo -e "${BOLD}=== Pairing ${name} (${mac}) ===${NC}" >&2
    echo "" >&2

    if is_paired "$mac" && is_trusted "$mac"; then
        ok "Already paired and trusted. Nothing to do."
        return 0
    fi

    if is_paired "$mac"; then
        warn "Device is paired but not trusted."
        read -rp "    Re-pair from scratch? [y/N] " answer
        if [[ "$answer" =~ ^[Yy]$ ]]; then
            unpair_device "$mac"
        else
            info "Trusting device..."
            bluetoothctl trust "$mac" 2>/dev/null
            ok "Device trusted."
            return 0
        fi
    fi

    # Pair — bonding needs a registered agent (agent + default-agent);
    # a bare `bluetoothctl pair` has none and fails on some models.
    info "Pairing with $mac..."
    echo "    Press the toothbrush's power button now so it is awake." >&2
    echo "    This may take a few seconds..." >&2

    local pair_output
    pair_output=$( {
        echo "agent KeyboardDisplay"
        sleep 0.5
        echo "default-agent"
        sleep 0.5
        echo "pair $mac"
        # LE Secure Connections pairing + GATT discovery takes several seconds
        sleep 15
        echo "quit"
    } | bluetoothctl 2>&1 | grep -v "^\[DEL\]\|^\[NEW\]\|^	" ) || true

    if echo "$pair_output" | grep -q "Pairing successful"; then
        ok "Pairing successful!"
    elif is_paired "$mac"; then
        ok "Pairing successful!"
    else
        local fail_reason
        # Portable extraction: BusyBox grep (HA Terminal add-on) lacks -P/\K
        fail_reason=$(echo "$pair_output" | sed -n 's/.*Failed to pair: //p' | head -n1)
        [ -z "$fail_reason" ] && fail_reason="unknown"
        err "Pairing failed: $fail_reason"
        echo "" >&2
        if echo "$fail_reason" | grep -qi "AuthenticationFailed"; then
            echo "    The toothbrush may have a stale bond from a previous pairing." >&2
            echo "    Try: press the power button to turn it off, wait a few" >&2
            echo "    seconds, turn it back on, then run this script again." >&2
        else
            echo "    Make sure:" >&2
            echo "    1. The toothbrush is awake (press the power button —" >&2
            echo "       it is NOT reachable while on the charger)" >&2
            echo "    2. The toothbrush is NOT connected to a phone" >&2
            echo "       (close the Philips Sonicare app first)" >&2
            echo "    3. The toothbrush is within Bluetooth range" >&2
        fi
        return 1
    fi

    # Trust
    info "Trusting device for auto-reconnection..."
    bluetoothctl trust "$mac" >/dev/null 2>&1
    ok "Device trusted."

    # Disconnect (HA will reconnect on its own)
    # Subshell suppresses bash's segfault message; redirects suppress GATT cache dump
    (bluetoothctl disconnect "$mac" >/dev/null 2>&1 || true) 2>/dev/null

    echo "" >&2
    ok "${name} is ready for Home Assistant!"
}

# --- main ---

echo "" >&2
echo -e "${BOLD}Philips Sonicare BLE Pairing${NC}" >&2
echo "────────────────────────────────────" >&2
echo "" >&2

# If MAC provided as argument, pair directly
if [[ ${1:-} =~ ^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$ ]]; then
    mac="${1^^}"  # uppercase
    # Unknown to BlueZ yet? Scan so the device path exists before pairing.
    if ! bluetoothctl info "$mac" &>/dev/null; then
        scan_for_devices
        if ! bluetoothctl info "$mac" &>/dev/null; then
            err "Device $mac was not seen during the scan."
            echo "    Wake the toothbrush (press the power button) and try again." >&2
            exit 1
        fi
    fi
    name=$(bluetoothctl info "$mac" 2>/dev/null | grep "Name:" | sed 's/.*Name: //')
    name=${name:-"Unknown Device"}
    pair_device "$mac" "$name"
    exit $?
fi

# Scan, then find Sonicare devices from bluetoothctl's device cache
scan_for_devices
devices=$(find_sonicare_devices)

if [[ -z "$devices" ]]; then
    warn "No Sonicare toothbrushes found."
    echo "" >&2
    echo "Troubleshooting:" >&2
    echo "  1. Wake the toothbrush right before scanning — press the power" >&2
    echo "     button or lift it off the charger (it is NOT reachable while" >&2
    echo "     charging, and goes back to sleep after ~20 seconds)" >&2
    echo "  2. Move closer to the Bluetooth adapter" >&2
    echo "  3. Close the Philips Sonicare app on your phone first" >&2
    echo "  4. Try running the scan again" >&2
    echo "" >&2
    echo "You can also pair a known address directly:" >&2
    echo "  $0 AA:BB:CC:11:22:33" >&2
    exit 1
fi

# Display found devices
echo -e "${BOLD}Found Sonicare toothbrushes:${NC}" >&2
echo "" >&2

declare -a macs=()
declare -a names=()
i=1
while IFS=$'\t' read -r mac name; do
    status_text=""
    if is_paired "$mac" && is_trusted "$mac"; then
        status_text=" ${GREEN}(paired & trusted)${NC}"
    elif is_paired "$mac"; then
        status_text=" ${YELLOW}(paired, not trusted)${NC}"
    fi

    echo -e "  ${BOLD}${i})${NC} ${name} (${mac})${status_text}" >&2
    macs+=("$mac")
    names+=("$name")
    ((i++))
done <<< "$devices"

echo "" >&2

if [[ ${#macs[@]} -eq 1 ]]; then
    read -rp "Pair this device? [Y/n] " answer
    if [[ "$answer" =~ ^[Nn]$ ]]; then
        echo "Cancelled." >&2
        exit 0
    fi
    pair_device "${macs[0]}" "${names[0]}"
else
    echo "  a) Pair all devices" >&2
    echo "" >&2
    read -rp "Select device (1-${#macs[@]}, a=all): " choice

    if [[ "$choice" == "a" || "$choice" == "A" ]]; then
        for idx in "${!macs[@]}"; do
            pair_device "${macs[$idx]}" "${names[$idx]}"
        done
    elif [[ "$choice" =~ ^[0-9]+$ ]] && (( choice >= 1 && choice <= ${#macs[@]} )); then
        idx=$((choice - 1))
        pair_device "${macs[$idx]}" "${names[$idx]}"
    else
        err "Invalid selection."
        exit 1
    fi
fi

echo "" >&2
echo "────────────────────────────────────" >&2
echo -e "${BOLD}Next steps:${NC}" >&2
echo "  1. Go to Home Assistant → Settings → Devices & Services" >&2
echo "  2. The toothbrush should appear under 'Discovered'" >&2
echo "  3. Or click 'Add Integration' → 'Philips Sonicare'" >&2
echo "" >&2
