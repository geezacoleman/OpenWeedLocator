#!/usr/bin/env bash
# Hailo-8 / Hailo-8L AI accelerator: install the driver and runtime, or check a unit that has them.
#
#   sudo bash tools/hailo_setup.sh             install (bash owl_setup.sh --hailo runs this)
#   sudo bash tools/hailo_setup.sh --check     verify after the reboot: enumeration, driver,
#                                              firmware, then the streaming benchmark 3 times
#   --runs N        benchmark runs for --check (default 3, 0 skips the benchmark)
#   --kv            --check prints key=value lines only (for scripts)
#
# Supported: the Hailo-8 (M.2 module, AI HAT+ 26T) and the Hailo-8L (Raspberry Pi AI Kit,
# AI HAT+ 13T) on a Raspberry Pi 5 or Compute Module 5: one driver line, HailoRT 4.23.
# The Hailo-10H (AI HAT+ 2) needs HailoRT 5.x, which cannot share an image with 4.x: it is
# not set up here (utils/ai_capability.py reports it so the screens say "not supported").
# docs/HAILO.md has the rest. Also run as root from /usr/local/lib/owl by
# owl_update.sh --with-hailo (Set up Hailo in the dashboard and the app).
#
# What install changes, all safe to re-run:
#   /boot/firmware/config.txt    dtparam=pciex1 under [all]. A Compute Module autodetects
#                                nothing, so the PCIe lane is switched on explicitly.
#                                PCIe stays at Gen 2: the CM5 is specified for Gen 2 and the
#                                OWL carrier is not stable at Gen 3. A dtparam=pciex1_gen=3
#                                line is reported, never added.
#   /boot/firmware/cmdline.txt   pcie_aspm=off (the state Hailo recommends)
#   apt                          dkms, pciutils, hailo-all
#   driver                       tools/patch_hailo_dkms.sh: the 4.23.0 DKMS driver calls
#                                find_vma() without the mmap lock, which kernel 6.18 refuses,
#                                so every streaming transfer fails. The patch adds the lock,
#                                rebuilds the module and holds the Hailo packages, because an
#                                apt upgrade would put the unpatched source back.
#
# Installing this does not make detection use the Hailo: that is utils/greenongreen.py's job.
#
# Exit: 0 done or check passed, 1 failed (the last lines say where).

set -u

R="${HAILO_SETUP_ROOT:-}"                       # tests point this at a scratch directory
BOOT_CONFIG="$R/boot/firmware/config.txt"
CMDLINE="$R/boot/firmware/cmdline.txt"
MODEL_FILE="$R/proc/device-tree/model"
SRC_DIR="$R/usr/src"
MODULES_DIR="$R/lib/modules"
DEVICE="$R/dev/hailo0"
# The self-test models (stock Hailo builds, never used for spraying)
HEF_DIR="$R/usr/share/hailo-models"
PATCH_SCRIPT="${HAILO_PATCH_SCRIPT:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/patch_hailo_dkms.sh}"
MARKER="# OWL Hailo-8 (tools/hailo_setup.sh)"
BENCHMARK_TIMEOUT_S=300

MODE=install
RUNS=3
KV=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --check) MODE=check ;;
        --kv)    KV=1 ;;
        --runs)  RUNS="${2:-}"; shift ;;
        *) echo "[ERROR] Unknown option '$1' (supported: --check [--runs N] [--kv])" >&2; exit 1 ;;
    esac
    shift
done
if ! [[ "$RUNS" =~ ^[0-9]+$ ]]; then
    echo "[ERROR] --runs needs a number" >&2
    exit 1
fi

if [[ $EUID -ne 0 && -z "$R" ]]; then
    echo "[ERROR] run with sudo: sudo bash tools/hailo_setup.sh" >&2
    exit 1
fi

# In --kv mode the words go to stderr so stdout is key=value only
say()  { if [[ "$KV" == "1" ]]; then echo "$*" >&2; else echo "$*"; fi; }
kv()   { if [[ "$KV" == "1" ]]; then printf '%s=%s\n' "$1" "$2"; fi; }
fail() { say "[FAIL] $*"; exit 1; }

dkms_module() {
    # "hailo_pci/4.23.0" from dkms status, or nothing
    dkms status 2>/dev/null | awk -F'[/, ]' '/hailo/ { print $1 "/" $2; exit }'
}

driver_built() {
    dkms status 2>/dev/null | grep -i hailo | grep -F "$(uname -r)" | grep -q installed
}

driver_patched() {
    # yes | no | unknown (no driver source on this unit). The same test the patch script uses.
    local dir file seen=0
    for dir in "$SRC_DIR"/hailo_pci-* "$SRC_DIR"/hailort-pcie-driver; do
        file="$dir/linux/vdma/memory.c"
        [[ -f "$file" ]] || continue
        seen=1
        if ! sed -n '160,200p' "$file" | grep -q 'mmap_read_lock(current->mm);'; then
            echo no
            return
        fi
    done
    if [[ "$seen" == "1" ]]; then echo yes; else echo unknown; fi
}

# ---------------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------------
install_hailo() {
    local model
    model="$(tr -d '\0' < "$MODEL_FILE" 2>/dev/null)"
    case "$model" in
        *"Compute Module 5"*|*"Raspberry Pi 5"*) ;;
        *) fail "a Hailo needs a Raspberry Pi 5 or Compute Module 5 (this is: ${model:-unknown})" ;;
    esac

    [[ -f "$BOOT_CONFIG" ]] || fail "$BOOT_CONFIG not found"
    [[ -f "$CMDLINE" ]] || fail "$CMDLINE not found"

    if grep -qE '^dtparam=pciex1_gen=3' "$BOOT_CONFIG"; then
        say "[WARN] config.txt forces PCIe Gen 3 (dtparam=pciex1_gen=3). The CM5 is specified for"
        say "       Gen 2 and the OWL carrier drops the Hailo at Gen 3. Remove that line if the"
        say "       check fails."
    fi
    if grep -qF "$MARKER" "$BOOT_CONFIG"; then
        say "[OK] PCIe already switched on in config.txt"
    else
        printf '\n%s\n[all]\ndtparam=pciex1\n' "$MARKER" >> "$BOOT_CONFIG" \
            || fail "could not write to $BOOT_CONFIG"
        say "[OK] PCIe switched on in config.txt (dtparam=pciex1, Gen 2)"
    fi

    if grep -qE '(^|[[:space:]])pcie_aspm=off([[:space:]]|$)' "$CMDLINE"; then
        say "[OK] pcie_aspm=off already in cmdline.txt"
    else
        if grep -qE '(^|[[:space:]])pcie_aspm=' "$CMDLINE"; then
            sed -i -E 's/(^|[[:space:]])pcie_aspm=[^[:space:]]*/\1pcie_aspm=off/' "$CMDLINE"
        else
            sed -i '1 s/[[:space:]]*$/ pcie_aspm=off/' "$CMDLINE"
        fi
        grep -qE '(^|[[:space:]])pcie_aspm=off([[:space:]]|$)' "$CMDLINE" \
            || fail "could not add pcie_aspm=off to $CMDLINE"
        say "[OK] pcie_aspm=off added to cmdline.txt"
    fi
    # cmdline.txt is one line: a second line is ignored by the firmware
    [[ "$(grep -c . "$CMDLINE")" == "1" ]] || fail "$CMDLINE has more than one line: fix it by hand"

    # One driver line per image: HailoRT 4.x (Hailo-8) and 5.x (Hailo-10H) cannot share one
    if dpkg -s hailo-h10-all >/dev/null 2>&1; then
        fail "hailo-h10-all (Hailo-10H) is installed. It cannot share an image with the Hailo-8/8L runtime"
    fi
    if dpkg -s hailo-all >/dev/null 2>&1; then
        say "[OK] hailo-all already installed"
    else
        say "[INFO] Installing dkms, pciutils and hailo-all..."
        apt-get update || say "[WARN] apt-get update failed: trying the install with the lists already here"
        apt-get install -y dkms pciutils hailo-all || fail "apt could not install hailo-all"
        say "[OK] hailo-all installed"
    fi

    if [[ "$(driver_patched)" == "yes" ]] && driver_built; then
        say "[OK] driver already patched and built for $(uname -r)"
    else
        [[ -f "$PATCH_SCRIPT" ]] || fail "$PATCH_SCRIPT not found"
        say "[INFO] Patching and rebuilding the Hailo driver..."
        bash "$PATCH_SCRIPT" \
            || fail "the driver patch did not go on. AI on the Hailo will not work until it does (see the lines above)"
        say "[OK] driver patched and rebuilt for $(uname -r)"
    fi

    # The patch script builds for the kernel that is running. A kernel installed by an
    # upgrade and not booted yet still has the module built from the unpatched source.
    local module kernel
    module="$(dkms_module)"
    if [[ -n "$module" ]]; then
        for kernel in "$MODULES_DIR"/*/; do
            kernel="$(basename "$kernel")"
            [[ "$kernel" != "$(uname -r)" && -e "$MODULES_DIR/$kernel/build" ]] || continue
            if dkms build "$module" -k "$kernel" --force >/dev/null 2>&1 \
                    && dkms install "$module" -k "$kernel" --force >/dev/null 2>&1; then
                say "[OK] driver rebuilt for $kernel too"
            else
                say "[WARN] driver not rebuilt for $kernel. If the unit boots that kernel, run this again"
            fi
        done
    fi

    local held
    held="$(apt-mark showhold 2>/dev/null | grep -i hailo | tr '\n' ' ')"
    if [[ -n "$held" ]]; then
        say "[OK] held so an upgrade cannot undo the patch: $held"
    else
        say "[WARN] no Hailo package is held. Run: dpkg -l | grep -i hailo, then sudo apt-mark hold <driver package>"
    fi

    say ""
    say "[OK] Hailo set up. Reboot, then check it: sudo bash tools/hailo_setup.sh --check"
    return 0
}

# ---------------------------------------------------------------------------------
# Check: stop at the first failure, the stage says which layer is at fault
# ---------------------------------------------------------------------------------
check_fail() {
    kv result fail
    kv stage "$1"
    kv reason "$2"
    say "[FAIL] $1: $2"
    exit 1
}

check_hailo() {
    local card link patched chip out run rc fps all_fps="" rwsem throttled hef

    # 1. Enumeration. Nothing here is wiring, power or config.txt, never software
    card="$(lspci -d 1e60: 2>/dev/null | head -n 1)"
    [[ -n "$card" ]] || check_fail enumeration "no Hailo card on PCIe (lspci). Carrier, power or config.txt, not software"
    kv card "$card"
    link="$(lspci -vv -d 1e60: 2>/dev/null | sed -n 's/.*LnkSta:[[:space:]]*Speed \([^ ,]*\).*/\1/p' | head -n 1)"
    kv link "${link:-unknown}"
    say "[OK] 1. card on PCIe, link ${link:-speed unknown}"
    if [[ "$link" == "8GT/s" ]]; then
        say "[WARN] the link is at Gen 3. The OWL carrier is only stable at Gen 2"
    fi

    # 2. Driver
    [[ -e "$DEVICE" ]] || check_fail driver "$DEVICE is missing: the driver did not load (dmesg | grep -i hailo)"
    driver_built || check_fail driver "dkms does not show the hailo module installed for $(uname -r)"
    patched="$(driver_patched)"
    kv patch "$patched"
    [[ "$patched" != "no" ]] || check_fail driver "the driver source is not patched. Run: sudo bash tools/hailo_setup.sh"
    kv held "$(apt-mark showhold 2>/dev/null | grep -i hailo | tr '\n' ' ')"
    say "[OK] 2. driver loaded, built for $(uname -r), patch: $patched"

    # 3. Firmware
    out="$(timeout 20 hailortcli fw-control identify 2>&1)"
    chip="$(printf '%s\n' "$out" | sed -n 's/.*Architecture:[[:space:]]*//p' | head -n 1 | tr 'A-Z' 'a-z' | tr -d ' -')"
    kv chip "${chip:-unknown}"
    case "$chip" in
        hailo8|hailo8l) ;;
        hailo10h) check_fail firmware "this is a Hailo-10H: it needs HailoRT 5.x, which the OWL does not set up" ;;
        *) check_fail firmware "hailortcli fw-control identify reports '${chip:-nothing}', not hailo8 or hailo8l" ;;
    esac
    say "[OK] 3. firmware answers: $chip"

    # 4. Streaming benchmark. Once is not enough: a unit that passes once and not three
    # times has a power or thermal problem
    if [[ "$RUNS" -gt 0 ]]; then
        # An 8L cannot run a hailo8 build; the 8L build runs on both
        if [[ "$chip" == "hailo8l" ]]; then hef="$HEF_DIR/yolov8s_h8l.hef"; else hef="$HEF_DIR/yolov8s_h8.hef"; fi
        hef="${HAILO_BENCHMARK_HEF:-$hef}"
        [[ -f "$hef" ]] || check_fail benchmark "$hef not found"
        for run in $(seq 1 "$RUNS"); do
            out="$(timeout "$BENCHMARK_TIMEOUT_S" hailortcli benchmark "$hef" 2>&1)"
            rc=$?
            fps="$(printf '%s\n' "$out" | grep -i '(streaming)' | grep -Eo '[0-9]+(\.[0-9]+)?' | tail -n 1)"
            if [[ "$rc" -ne 0 ]]; then
                say "$(printf '%s\n' "$out" | tail -n 6)"
                check_fail benchmark "run $run of $RUNS failed (exit $rc). Read dmesg now, before rebooting"
            fi
            all_fps+="${all_fps:+,}${fps:-unknown}"
            say "[OK] 4. benchmark run $run of $RUNS: ${fps:-unknown} fps streaming"
        done
        kv runs "$RUNS"
        kv fps "$all_fps"
        rwsem="$(dmesg 2>/dev/null | grep -c rwsem)"
        kv rwsem "$rwsem"
        [[ "$rwsem" == "0" ]] \
            || check_fail benchmark "dmesg has $rwsem rwsem warning(s): the unpatched driver is the one loaded"
        throttled="$(vcgencmd get_throttled 2>/dev/null | sed 's/^throttled=//')"
        kv throttled "${throttled:-unknown}"
        [[ -z "$throttled" || "$throttled" == "0x0" ]] \
            || check_fail benchmark "the Pi reports power trouble under load (get_throttled $throttled)"
    else
        kv runs 0
        say "[INFO] benchmark skipped (--runs 0)"
    fi

    kv result pass
    say "[PASS] $chip ready"
    return 0
}

if [[ "$MODE" == "check" ]]; then
    check_hailo
else
    install_hailo
fi
