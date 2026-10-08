#!/bin/bash
# =============================================================================
# owl_identity.sh — the unit's hardware identity (OWL trust chain, MVP)
# =============================================================================
#
# A production OWL proves which physical board it is with an ECDSA P-256 key
# that the CM5 firmware holds in one-time-programmable memory (OTP key slot 1)
# and only ever uses to sign — Linux never sees the private key. Maker units on
# a Pi 4/5 without that firmware path use a P-256 key file instead.
#
# Everything this script writes lives in /etc/owl-identity:
#   device.pub        P-256 public key, SPKI PEM (0644)
#   device.key        private key, DISK tier only (0600 root)
#   tier              otp | disk
#   noktura_serial    OWL-YYYY-NNNNNN issued by Noktura at registration (public)
#
# USAGE (root)
#   sudo bash owl_identity.sh --otp             CM5: generate the key in OTP slot 1
#   sudo bash owl_identity.sh --disk            Pi 4/5: P-256 key file
#   sudo bash owl_identity.sh --serial OWL-2026-000001
#   sudo bash owl_identity.sh --show            JSON: hw_id, tier, fingerprint, serial, PEM
#   sudo bash owl_identity.sh --sign FILE       DER signature of FILE to stdout (spike/bench)
#
# --otp is PERMANENT: the BCM2712 has exactly one key slot and OTP bits cannot
# be cleared. It asks for a typed confirmation. It is idempotent: a populated
# slot is read, not rewritten (the jig's `oem fwcrypto init` behaves the same).
#
# Spec: Noktura/docs/owl/OWL_IDENTITY_AND_CLAIM_SPEC.md §3.
# =============================================================================
set -euo pipefail

GREEN='\033[0;32m'; RED='\033[0;31m'; ORANGE='\033[0;33m'; NC='\033[0m'

IDENTITY_DIR="${OWL_IDENTITY_DIR:-/etc/owl-identity}"
KEY_ID=1
FW_CRYPTO="${OWL_FW_CRYPTO:-rpi-fw-crypto}"
BOOT_CONFIG="${OWL_BOOT_CONFIG:-/boot/firmware/config.txt}"

usage() { sed -n '2,28p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

fail() { echo -e "${RED}[ERROR] $*${NC}" >&2; exit 1; }
info() { echo -e "${GREEN}[INFO] $*${NC}"; }
warn() { echo -e "${ORANGE}[WARN] $*${NC}"; }

need_root() { [[ "$(id -u)" == "0" ]] || fail "Run with sudo: sudo bash $0 $*"; }

hw_id() {
    # Pi hardware serial, same source as utils/device_identity.get_device_serial
    local serial
    serial=$(awk -F': ' '/^Serial/ {print $2}' /proc/cpuinfo 2>/dev/null | tr -d ' \t')
    if [[ -z "$serial" || "$serial" =~ ^0+$ ]]; then
        serial=$(cat /etc/machine-id 2>/dev/null || true)
    fi
    printf '%s' "$serial"
}

fingerprint_of() {
    # SHA-256 (hex) of the DER SPKI — the value Noktura stores and the app compares.
    openssl pkey -pubin -in "$1" -outform DER 2>/dev/null | sha256sum | cut -d' ' -f1
}

check_p256() {
    # The OTP slot and Noktura both require prime256v1. Refuse anything else.
    local curve
    curve=$(openssl pkey -pubin -in "$1" -text -noout 2>/dev/null | awk -F': ' '/ASN1 OID/ {print $2}')
    [[ "$curve" == "prime256v1" ]] || fail "public key is not P-256 (got '${curve:-unknown}')"
}

write_common() {
    install -d -m 755 "$IDENTITY_DIR"
    chmod 644 "$IDENTITY_DIR/device.pub"
    printf '%s\n' "$1" > "$IDENTITY_DIR/tier"
    chmod 644 "$IDENTITY_DIR/tier"
}

ensure_read_lock() {
    # Permanent read lock on the OTP private key. Runtime locks from
    # `set-key-status` reset at reboot; only config.txt survives.
    if [[ ! -w "$BOOT_CONFIG" ]]; then
        warn "cannot write $BOOT_CONFIG; add 'lock_device_private_key=1' under [all] by hand"
        return
    fi
    if grep -q '^lock_device_private_key=1' "$BOOT_CONFIG"; then
        info "config.txt already has lock_device_private_key=1"
        return
    fi
    if grep -q '^\[all\]' "$BOOT_CONFIG"; then
        printf '\n# OWL identity: the OTP private key signs only, it is never read out.\nlock_device_private_key=1\n' >> "$BOOT_CONFIG"
    else
        printf '\n[all]\n# OWL identity: the OTP private key signs only, it is never read out.\nlock_device_private_key=1\n' >> "$BOOT_CONFIG"
    fi
    info "lock_device_private_key=1 appended to $BOOT_CONFIG (takes effect after reboot)"
}

do_otp() {
    need_root --otp
    command -v "$FW_CRYPTO" >/dev/null 2>&1 || fail "$FW_CRYPTO not found. Install the rpi-fw-crypto package, or build raspberrypi/utils/rpifwcrypto (cmake). See PROJECT_NOTES for the bench record."
    [[ -e /dev/vcio_crypto || -e /dev/vcio ]] || fail "no firmware crypto device (/dev/vcio_crypto); is this a Raspberry Pi 5 / CM5?"

    install -d -m 755 "$IDENTITY_DIR"
    local status
    status=$("$FW_CRYPTO" get-key-status --key-id "$KEY_ID" 2>&1 || true)
    info "key slot $KEY_ID status: ${status:-unknown}"

    if ! "$FW_CRYPTO" pubkey --key-id "$KEY_ID" > "$IDENTITY_DIR/device.pub.tmp" 2>/dev/null \
       || ! grep -q 'BEGIN PUBLIC KEY' "$IDENTITY_DIR/device.pub.tmp"; then
        # Blank slot. Generating is permanent: confirm.
        rm -f "$IDENTITY_DIR/device.pub.tmp"
        echo -e "${ORANGE}[WARN] OTP key slot $KEY_ID is blank. Generating a key writes OTP bits that${NC}"
        echo -e "${ORANGE}       can never be cleared. This board will carry this identity for life.${NC}"
        if [[ "${OWL_IDENTITY_YES:-}" != "1" ]]; then
            read -r -p "Type GENKEY to continue: " confirm
            [[ "$confirm" == "GENKEY" ]] || fail "aborted, nothing written"
        fi
        "$FW_CRYPTO" genkey --key-id "$KEY_ID" || fail "genkey failed"
        "$FW_CRYPTO" pubkey --key-id "$KEY_ID" > "$IDENTITY_DIR/device.pub.tmp" || fail "pubkey after genkey failed"
        info "P-256 key generated in OTP slot $KEY_ID"
    else
        info "OTP slot $KEY_ID already holds a key; using it"
    fi
    check_p256 "$IDENTITY_DIR/device.pub.tmp"
    mv "$IDENTITY_DIR/device.pub.tmp" "$IDENTITY_DIR/device.pub"
    rm -f "$IDENTITY_DIR/device.key"
    write_common otp
    ensure_read_lock
    do_show
}

do_disk() {
    need_root --disk
    install -d -m 755 "$IDENTITY_DIR"
    if [[ -s "$IDENTITY_DIR/device.key" ]]; then
        info "disk key already present; keeping it"
    else
        (umask 077; openssl ecparam -name prime256v1 -genkey -noout -out "$IDENTITY_DIR/device.key")
        info "P-256 key file created"
    fi
    chmod 600 "$IDENTITY_DIR/device.key"
    openssl pkey -in "$IDENTITY_DIR/device.key" -pubout -out "$IDENTITY_DIR/device.pub"
    check_p256 "$IDENTITY_DIR/device.pub"
    write_common disk
    do_show
}

do_serial() {
    need_root --serial
    local serial="${1:-}"
    [[ "$serial" =~ ^OWL-[0-9]{4}-[0-9]{6}$ ]] || fail "serial must look like OWL-2026-000001"
    install -d -m 755 "$IDENTITY_DIR"
    printf '%s\n' "$serial" > "$IDENTITY_DIR/noktura_serial"
    chmod 644 "$IDENTITY_DIR/noktura_serial"
    info "Noktura serial recorded: $serial"
}

do_sign() {
    # Bench spike: prove the firmware signs and OpenSSL verifies.
    #   sudo bash owl_identity.sh --sign payload.bin > sig.der
    #   openssl dgst -sha256 -verify /etc/owl-identity/device.pub -signature sig.der payload.bin
    local file="${1:-}"
    [[ -s "$file" ]] || fail "--sign needs a non-empty file"
    local tier
    tier=$(cat "$IDENTITY_DIR/tier" 2>/dev/null || echo none)
    case "$tier" in
        otp)  "$FW_CRYPTO" sign --in "$file" --key-id "$KEY_ID" --alg ec ;;
        disk) openssl dgst -sha256 -sign "$IDENTITY_DIR/device.key" "$file" ;;
        *)    fail "no identity on this unit (run --otp or --disk first)" ;;
    esac
}

do_show() {
    local tier pub fp serial pem
    tier=$(cat "$IDENTITY_DIR/tier" 2>/dev/null || echo none)
    pub="$IDENTITY_DIR/device.pub"
    if [[ -s "$pub" ]]; then
        fp=$(fingerprint_of "$pub")
        pem=$(cat "$pub")
    else
        fp=""; pem=""
    fi
    serial=$(cat "$IDENTITY_DIR/noktura_serial" 2>/dev/null || true)
    # Minimal JSON by hand (no python dependency for a root shell tool). The
    # PEM contains only base64, newlines and dashes.
    printf '{"hw_id": "%s", "tier": "%s", "fingerprint": "%s", "noktura_serial": "%s", "public_key_pem": "%s"}\n' \
        "$(hw_id)" "$tier" "$fp" "$serial" "$(printf '%s' "$pem" | sed ':a;N;$!ba;s/\n/\\n/g')"
}

case "${1:-}" in
    --otp)    do_otp ;;
    --disk)   do_disk ;;
    --serial) do_serial "${2:-}" ;;
    --sign)   do_sign "${2:-}" ;;
    --show)   do_show ;;
    -h|--help|"") usage 0 ;;
    *) echo -e "${RED}[ERROR] Unknown option '$1'${NC}" >&2; usage 2 ;;
esac
