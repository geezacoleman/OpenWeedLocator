#!/bin/bash
# =============================================================================
# tools/noktura_register.sh — register an OWL with Noktura from the BENCH LAPTOP
# =============================================================================
#
# Runs on Guy's laptop, never on the unit: the factory token must not touch a
# device. Takes the unit's identity (from `sudo bash owl_identity.sh --show` on
# the unit), registers it with POST /api/v1/factory/devices/provision, optionally
# marks it ready to ship (qa_pass), and writes a claim card the tester gets in
# the box. This script is the model for the CM5 jig's post-flash hook later.
#
# USAGE
#   tools/noktura_register.sh --identity identity.json --batch PROTO-2026-09 \
#       --token-file ~/.noktura/factory-token [--ready] [--api https://app.noktura.tech]
#
#   identity.json = the JSON printed by `owl_identity.sh --show` on the unit.
#
# Prints the serial and (on first registration) the claim code, and writes
# claim_card_<serial>.html next to the identity file. Nothing needs writing back
# to the unit: the OWL app looks the serial up in Noktura by key fingerprint
# (Noktura spec §5.13). Optional local label for bench --show output:
#   sudo bash owl_identity.sh --serial <serial>
#
# Re-runs are safe: same hw_id + same key returns the same serial. Once the unit
# is past `provisioned` (ready_to_ship, claimed) the response carries no claim
# code (N13) — the card cannot be reprinted, only rotated (support path).
#
# Never send Idempotency-Key from here or from the jig hook: Noktura hashes the
# body and a changed image hash would turn a re-flash into a 409.
# Spec: Noktura/docs/owl/OWL_IDENTITY_AND_CLAIM_SPEC.md §5.3, §5.8.
# =============================================================================
set -euo pipefail

API="https://app.noktura.tech"
IDENTITY=""
BATCH=""
TOKEN_FILE=""
READY=0
FIRMWARE=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --identity)   IDENTITY="$2"; shift 2 ;;
        --batch)      BATCH="$2"; shift 2 ;;
        --token-file) TOKEN_FILE="$2"; shift 2 ;;
        --api)        API="${2%/}"; shift 2 ;;
        --firmware)   FIRMWARE="$2"; shift 2 ;;
        --ready)      READY=1; shift ;;
        -h|--help)    sed -n '2,29p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 2 ;;
    esac
done

for tool in curl jq; do
    command -v "$tool" >/dev/null 2>&1 || { echo "[ERROR] $tool is required" >&2; exit 1; }
done
[[ -s "$IDENTITY" ]] || { echo "[ERROR] --identity file missing or empty" >&2; exit 2; }
[[ -n "$BATCH" ]] || { echo "[ERROR] --batch is required (e.g. PROTO-2026-09)" >&2; exit 2; }
[[ -s "$TOKEN_FILE" ]] || { echo "[ERROR] --token-file missing (mint one with apps/web/scripts/mint-factory-token.ts)" >&2; exit 2; }
TOKEN="$(tr -d '\r\n' < "$TOKEN_FILE")"
[[ "$TOKEN" == nkt_factory_* ]] || { echo "[ERROR] token file does not hold a factory token" >&2; exit 2; }

HW_ID=$(jq -r '.hw_id' "$IDENTITY")
TIER=$(jq -r '.tier' "$IDENTITY")
PEM=$(jq -r '.public_key_pem' "$IDENTITY")
FP=$(jq -r '.fingerprint' "$IDENTITY")
[[ "$HW_ID" =~ ^[0-9a-f]{8,32}$ ]] || { echo "[ERROR] hw_id '$HW_ID' does not look like a Pi serial" >&2; exit 2; }
[[ "$TIER" == "otp" || "$TIER" == "disk" ]] || { echo "[ERROR] tier is '$TIER'; run owl_identity.sh --otp or --disk on the unit first" >&2; exit 2; }
[[ "$PEM" == *"BEGIN PUBLIC KEY"* ]] || { echo "[ERROR] identity has no public key" >&2; exit 2; }

BODY=$(jq -n --arg hw "$HW_ID" --arg pem "$PEM" --arg tier "$TIER" --arg batch "$BATCH" --arg fw "$FIRMWARE" \
    '{hw_id:$hw, public_key:$pem, identity_tier:$tier, device_type:"owl", factory_batch:$batch}
     + (if $fw != "" then {firmware_version:$fw} else {} end)')

echo "[INFO] Registering hw_id=$HW_ID tier=$TIER fingerprint=${FP:0:16}... with $API"
HTTP=$(curl -sS -o /tmp/noktura_provision.json -w '%{http_code}' -X POST "$API/api/v1/factory/devices/provision" \
    -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' --data "$BODY")
RESP=$(cat /tmp/noktura_provision.json); rm -f /tmp/noktura_provision.json

case "$HTTP" in
    201|200) ;;
    *) echo "[ERROR] provision failed: HTTP $HTTP $RESP" >&2; exit 1 ;;
esac

SERIAL=$(jq -r '.serial' <<<"$RESP")
CODE=$(jq -r '.claim_code // empty' <<<"$RESP")
STATE=$(jq -r '.state // "provisioned"' <<<"$RESP")
echo "[OK] serial=$SERIAL state=$STATE (HTTP $HTTP)"

if [[ "$READY" == "1" && "$STATE" == "provisioned" ]]; then
    HTTP2=$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$API/api/v1/factory/devices/$SERIAL/qa_pass" \
        -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
        --data '{"notes":"bench self-test passed (prototype)"}')
    if [[ "$HTTP2" == "200" ]]; then
        echo "[OK] $SERIAL marked ready_to_ship"
    else
        echo "[WARN] qa_pass returned HTTP $HTTP2 (unit stays $STATE)" >&2
    fi
fi

if [[ -n "$CODE" ]]; then
    CARD="$(dirname "$IDENTITY")/claim_card_${SERIAL}.html"
    HOTSPOT="OWL-$(printf '%s' "$HW_ID" | tail -c 4 | tr '[:lower:]' '[:upper:]')"
    cat > "$CARD" <<EOF
<!doctype html><meta charset="utf-8"><title>Claim card ${SERIAL}</title>
<style>body{font:16px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;max-width:560px;margin:40px auto;color:#16233a}
h1{font-size:22px;margin:0 0 4px}.k{font-family:ui-monospace,Consolas,monospace;font-size:28px;letter-spacing:.12em;border:1.5px solid #16233a;padding:10px 14px;display:inline-block;margin:8px 0}
dl{display:grid;grid-template-columns:max-content 1fr;gap:4px 16px}dt{color:#55657c}dd{margin:0;font-family:ui-monospace,Consolas,monospace}
.keep{background:#16233a;color:#fff;padding:8px 12px;font-weight:600;margin-top:16px}</style>
<h1>Add this OWL to your farm</h1>
<p>Serial <strong>${SERIAL}</strong></p>
<ol><li>Install the OWL app and sign in with your Noktura account.</li>
<li>Tap <strong>Add OWL</strong>, power on the OWL and join its hotspot when asked.</li>
<li>When the app asks for the claim code, type:</li></ol>
<div class="k">${CODE}</div>
<dl><dt>Hotspot</dt><dd>${HOTSPOT}</dd><dt>Hardware id</dt><dd>${HW_ID}</dd><dt>Batch</dt><dd>${BATCH}</dd></dl>
<div class="keep">Keep this card. Anyone with this code can claim the OWL.</div>
<p><small>Lost the card? Contact support: the code can be replaced, the old one stops working.</small></p>
EOF
    echo "[OK] claim card written: $CARD"
    echo "[NEXT] on the unit: bash owl_setup.sh --factory --yes (optional label first: sudo bash owl_identity.sh --serial $SERIAL)"
    echo "[NOTE] claim code is shown once: $CODE"
else
    echo "[INFO] no claim code returned (unit already past registration); nothing to print"
    echo "[INFO] optional local label: sudo bash owl_identity.sh --serial $SERIAL"
fi
