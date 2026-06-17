#!/bin/bash
# =============================================================================
# owl_update.sh — OWL software updater (manual + unattended)
# =============================================================================
#
# Updates the OWL repo to a named git ref (branch or tag) while preserving
# per-unit field configuration, then reinstalls Python dependencies and
# restarts the service. On ANY failure after changes begin, automatically
# rolls back to the pre-update state.
#
# USAGE
#   Manual (SSH):      bash owl_update.sh [--ref main]
#   Unattended (MQTT): launched via systemd-run by utils/mqtt_manager.py:
#                      owl_update.sh --unattended --ref main \
#                          --request-id <uuid> --status-file <path>
#
# OPTIONS
#   --ref REF          Branch or tag to update to (default: current branch)
#   --profile P        owl | controller (default: auto-detect)
#   --unattended       No prompts; failures roll back automatically
#   --request-id ID    Correlation id echoed into the status file
#   --status-file F    JSON status file path (default: <repo>/.update_status.json)
#   --no-restart       Skip the service restart + health check
#   --yes              Manual mode: don't prompt before restarting the service
#
# STATUS FILE
#   Written atomically before each phase; consumed by OWLMQTTPublisher and
#   published to the MQTT state topic. Terminal statuses: complete,
#   rolled_back, error.
#
# ON-DEVICE VERIFICATION CHECKLIST (cannot be tested off-device)
#   1. Manual run: same-branch, named-branch, tag, bogus ref
#   2. MQTT run: publish update_software, watch state.software_update through
#      the restart; confirm new git_commit in heartbeat
#   3. Rollback: branch with broken requirements.txt (pip fail); branch whose
#      owl.py exits at startup (crash-loop) — both must end 'rolled_back'
#      with field config intact
#   4. Conflict policy: locally modified GENERAL_CONFIG.ini survives update
#   5. Concurrency: second update while one runs is refused
#   6. kill -9 mid-run: next run must not be blocked by a stale lock
# =============================================================================

# --- Self-copy + re-exec -----------------------------------------------------
# git checkout rewrites this file mid-run; bash reads scripts incrementally,
# so the running copy must live outside the repo.
if [[ "${1:-}" != "--copied" ]]; then
    _SELF="$(realpath "${BASH_SOURCE[0]}")"
    _RUNCOPY="$(mktemp "${XDG_RUNTIME_DIR:-/tmp}/owl_update.XXXXXX.sh")"
    cp "$_SELF" "$_RUNCOPY"
    chmod +x "$_RUNCOPY"
    exec bash "$_RUNCOPY" --copied "$_SELF" "$@"
fi

set -Eeuo pipefail

ORIGINAL_SCRIPT="$2"
shift 2
RUNCOPY="$(realpath "${BASH_SOURCE[0]}")"
REPO_DIR="$(dirname "$ORIGINAL_SCRIPT")"
# Safe on Linux: bash holds an open fd to the copy, unlink doesn't affect it.
trap 'rm -f "$RUNCOPY"' EXIT

# --- Globals ------------------------------------------------------------------
TS="$(date +%Y%m%d_%H%M%S)"
REF=""
PROFILE=""
UNATTENDED=0
REQUEST_ID=""
STATUS_FILE="$REPO_DIR/.update_status.json"
NO_RESTART=0
ASSUME_YES=0

SERVICE=""
VENV=""
REQUIREMENTS=""

STARTED_AT="$(date +%s)"
FROM_DESC=""
TO_DESC=""

# Rollback bookkeeping
ROLLBACK_READY=0          # rollback point recorded (tag + config backup)
CHANGES_MADE=0            # repo has been mutated (checkout or beyond)
ROLLBACK_COMMIT=""
ROLLBACK_BRANCH=""        # empty => was detached
BACKUP_DIR=""
STASHED=0
IN_ROLLBACK=0

LOG_DIR="$REPO_DIR/logs/updates"
LOG_FILE="$LOG_DIR/owl_update_${TS}.log"

# --- Status file --------------------------------------------------------------
write_status() {
    local status="$1" error="${2:-}" rollback_failed="${3:-false}"
    # Built with system python3: the venv may be mid-reinstall, and python
    # handles JSON escaping of arbitrary error strings.
    /usr/bin/python3 - "$STATUS_FILE" <<'PYEOF' "$status" "$error" "$rollback_failed" "$REQUEST_ID" "$REF" "$FROM_DESC" "$TO_DESC" "$STARTED_AT" "$LOG_FILE"
import json, os, sys, time
path = sys.argv[1]
status, error, rollback_failed, request_id, ref, from_d, to_d, started, log = sys.argv[2:11]
payload = {
    "request_id": request_id,
    "status": status,
    "ref": ref,
    "from": from_d,
    "to": to_d,
    "error": error,
    "rollback_failed": rollback_failed == "true",
    "started_at": int(started),
    "updated_at": int(time.time()),
    "pid": os.getppid(),
    "log_file": log,
}
tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump(payload, f)
os.replace(tmp, path)
PYEOF
}

log() { echo "[$(date '+%H:%M:%S')] $*"; }

fail_terminal() {
    # Terminal error before any repo changes — no rollback needed.
    local msg="$1"
    log "ERROR: $msg"
    write_status error "$msg"
    exit 1
}

# --- Failure handling / rollback ----------------------------------------------
on_failure() {
    local line="$1" cmd="$2"
    trap - ERR
    set +e
    if [[ "$IN_ROLLBACK" == "1" ]]; then
        log "ERROR during rollback at line $line: $cmd"
        write_status error "rollback failed at: $cmd" true
        exit 1
    fi
    local original_error="failed at line $line: $cmd"
    log "ERROR: $original_error"
    if [[ "$CHANGES_MADE" == "1" && "$ROLLBACK_READY" == "1" ]]; then
        rollback "$original_error"
    else
        write_status error "$original_error"
        exit 1
    fi
}
trap 'on_failure "${BASH_LINENO[0]}" "$BASH_COMMAND"' ERR

rollback() {
    local original_error="$1"
    IN_ROLLBACK=1
    log "Rolling back to ${ROLLBACK_COMMIT:0:7}..."
    write_status rolling_back "$original_error"

    cd "$REPO_DIR"
    # Abort any half-finished stash merge state, then restore the tree.
    git merge --abort >/dev/null 2>&1 || true
    git reset --merge >/dev/null 2>&1 || true
    if [[ -n "$ROLLBACK_BRANCH" ]]; then
        git checkout -f "$ROLLBACK_BRANCH" || { write_status error "rollback: checkout $ROLLBACK_BRANCH failed (original: $original_error)" true; exit 1; }
        git reset --hard "$ROLLBACK_COMMIT" || { write_status error "rollback: reset failed (original: $original_error)" true; exit 1; }
    else
        git checkout -f --detach "$ROLLBACK_COMMIT" || { write_status error "rollback: detached checkout failed (original: $original_error)" true; exit 1; }
    fi
    # NO git clean — untracked field files (CONTROLLER.ini, models, creds) stay.

    # Field config restore: the backup, not the stash, is authoritative.
    if [[ -n "$BACKUP_DIR" && -d "$BACKUP_DIR" ]]; then
        cp -a "$BACKUP_DIR/." "$REPO_DIR/config/" || { write_status error "rollback: config restore failed (original: $original_error)" true; exit 1; }
    fi

    # Best-effort dependency restore from the rolled-back tree.
    "$VENV/bin/pip" install --no-input -r "$REPO_DIR/$REQUIREMENTS" >/dev/null 2>&1 || log "WARNING: pip restore failed (continuing)"

    if [[ "$NO_RESTART" != "1" ]]; then
        sudo -n systemctl restart "$SERVICE" || { write_status error "rollback: service restart failed (original: $original_error)" true; exit 1; }
        local i
        for i in $(seq 1 10); do
            sleep 3
            if systemctl is-active --quiet "$SERVICE"; then break; fi
        done
        if ! systemctl is-active --quiet "$SERVICE"; then
            write_status error "rollback: service not active after restart (original: $original_error)" true
            exit 1
        fi
    fi

    log "Rollback complete."
    write_status rolled_back "$original_error"
    exit 1
}

# --- Argument parsing -----------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --ref)          REF="$2"; shift 2 ;;
        --profile)      PROFILE="$2"; shift 2 ;;
        --unattended)   UNATTENDED=1; shift ;;
        --request-id)   REQUEST_ID="$2"; shift 2 ;;
        --status-file)  STATUS_FILE="$2"; shift 2 ;;
        --no-restart)   NO_RESTART=1; shift ;;
        --yes)          ASSUME_YES=1; shift ;;
        *) echo "Unknown option: $1" >&2; exit 2 ;;
    esac
done

main() {
    mkdir -p "$LOG_DIR"
    # Process substitution, not a pipeline — pipefail + tee would trip ERR.
    exec > >(tee -a "$LOG_FILE") 2>&1

    cd "$REPO_DIR"
    write_status starting
    log "OWL updater starting (repo: $REPO_DIR, log: $LOG_FILE)"

    # --- Lock (held until process exit; survives the service restart) ---------
    exec 9>"$REPO_DIR/.update_lock"
    if ! flock -n 9; then
        fail_terminal "another update is already in progress"
    fi

    # --- Profile detection -----------------------------------------------------
    if [[ -z "$PROFILE" ]]; then
        if systemctl cat owl-controller.service >/dev/null 2>&1 \
           && systemctl cat owl-controller.service | grep -q "$REPO_DIR"; then
            PROFILE="controller"
        elif systemctl cat owl.service >/dev/null 2>&1; then
            PROFILE="owl"
        else
            fail_terminal "cannot auto-detect profile (no owl.service or owl-controller.service) — pass --profile owl|controller"
        fi
    fi
    local user_home
    user_home="$(getent passwd "$(id -un)" | cut -d: -f6)"
    case "$PROFILE" in
        owl)
            SERVICE="owl.service"
            VENV="${OWL_VENV:-$user_home/.virtualenvs/owl}"
            REQUIREMENTS="requirements.txt"
            ;;
        controller)
            SERVICE="owl-controller.service"
            VENV="${OWL_VENV:-$user_home/controller_venv}"
            REQUIREMENTS="requirements-controller.txt"
            ;;
        *) fail_terminal "invalid profile: $PROFILE" ;;
    esac
    log "Profile: $PROFILE (service=$SERVICE, venv=$VENV, deps=$REQUIREMENTS)"

    # --- Preflight ---------------------------------------------------------------
    write_status preflight
    git rev-parse --is-inside-work-tree >/dev/null 2>&1 || fail_terminal "$REPO_DIR is not a git work tree"

    if [[ -z "$REF" ]]; then
        REF="$(git symbolic-ref -q --short HEAD || true)"
        [[ -n "$REF" ]] || fail_terminal "HEAD is detached and no --ref given"
    fi
    if ! [[ "$REF" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$ ]] || [[ "$REF" == *..* ]]; then
        fail_terminal "invalid ref: $REF"
    fi

    local avail_kb
    avail_kb="$(df --output=avail -k "$REPO_DIR" | tail -1 | tr -d ' ')"
    [[ "$avail_kb" -ge 1048576 ]] || fail_terminal "insufficient disk space (need 1 GB free, have $((avail_kb / 1024)) MB)"

    [[ -x "$VENV/bin/pip" ]] || fail_terminal "venv pip not found at $VENV/bin/pip"
    if [[ "$NO_RESTART" != "1" ]]; then
        systemctl cat "$SERVICE" >/dev/null 2>&1 || fail_terminal "$SERVICE not installed"
    fi

    # --- Fetch + resolve ref -------------------------------------------------------
    write_status fetching
    log "Fetching origin..."
    git fetch --prune --tags origin

    local ref_type=""
    if git ls-remote --exit-code --heads origin "$REF" >/dev/null 2>&1; then
        ref_type="branch"
    elif git ls-remote --exit-code --tags origin "$REF" >/dev/null 2>&1; then
        ref_type="tag"
    else
        fail_terminal "ref '$REF' not found on origin (branches and tags only)"
    fi

    local old_version old_branch old_commit
    old_version="$("$VENV/bin/python" -c 'import version; print(version.VERSION)' 2>/dev/null || echo unknown)"
    old_branch="$(git symbolic-ref -q --short HEAD || echo detached)"
    old_commit="$(git rev-parse --short HEAD)"
    FROM_DESC="${old_version}@${old_branch}/${old_commit}"
    log "Current: $FROM_DESC -> target: $REF ($ref_type)"

    # --- Rollback point ----------------------------------------------------------
    ROLLBACK_COMMIT="$(git rev-parse HEAD)"
    ROLLBACK_BRANCH="$(git symbolic-ref -q --short HEAD || true)"
    git tag -f "owl-pre-update-$TS" >/dev/null
    # Prune old rollback tags, keep newest 5
    git tag -l 'owl-pre-update-*' | sort | head -n -5 | xargs -r git tag -d >/dev/null

    BACKUP_DIR="$REPO_DIR/.update_backup/$TS"
    mkdir -p "$BACKUP_DIR"
    cp -a "$REPO_DIR/config/." "$BACKUP_DIR/"
    # Prune old backups, keep newest 3
    ls -1d "$REPO_DIR"/.update_backup/*/ 2>/dev/null | sort | head -n -3 | xargs -r rm -rf
    ROLLBACK_READY=1

    # --- Stash field config ---------------------------------------------------------
    write_status stashing
    STASHED=0
    if ! git diff-index --quiet HEAD --; then
        log "Stashing local modifications..."
        git stash push -m "owl-update-$TS"
        STASHED=1
    fi

    # --- Checkout -------------------------------------------------------------------
    write_status checking_out
    CHANGES_MADE=1
    if [[ "$ref_type" == "branch" ]]; then
        log "Checking out branch $REF (reset to origin/$REF)..."
        git checkout -B "$REF" "origin/$REF"
    else
        log "Checking out tag $REF (detached)..."
        git checkout --detach "refs/tags/$REF"
    fi

    # --- Restore field config ---------------------------------------------------------
    if [[ "$STASHED" == "1" ]]; then
        write_status restoring_config
        log "Re-applying field config..."
        local stash_sha
        stash_sha="$(git rev-parse -q --verify stash@{0} || true)"
        log "Stash SHA: $stash_sha"
        if ! git stash pop; then
            resolve_stash_conflicts
        fi
        # No unmerged paths may remain.
        if [[ -n "$(git diff --name-only --diff-filter=U)" ]]; then
            log "ERROR: unresolved conflicts remain"
            false  # -> ERR trap -> rollback
        fi
        # Pop-on-conflict keeps the entry; drop it if it's still there.
        if git rev-parse -q --verify stash@{0} >/dev/null 2>&1 \
           && [[ "$(git rev-parse -q --verify stash@{0})" == "$stash_sha" ]]; then
            git stash drop >/dev/null
        fi
        # Fleet steady state: field config as plain working-tree modifications.
        git restore --staged -- .
    fi

    # --- Dependencies ---------------------------------------------------------------
    write_status installing_deps
    log "Installing Python dependencies ($REQUIREMENTS)..."
    "$VENV/bin/pip" install --no-input -r "$REPO_DIR/$REQUIREMENTS"
    if [[ "$PROFILE" == "owl" && -f "$REPO_DIR/requirements-gog.txt" ]] \
       && "$VENV/bin/python" -c 'import ultralytics' >/dev/null 2>&1; then
        log "Green-on-Green detected — updating GoG dependencies..."
        "$VENV/bin/pip" install --no-input -r "$REPO_DIR/requirements-gog.txt"
    fi

    local new_version new_commit
    new_version="$("$VENV/bin/python" -c 'import version; print(version.VERSION)' 2>/dev/null || echo unknown)"
    new_commit="$(git rev-parse --short HEAD)"
    TO_DESC="${new_version}@${REF}/${new_commit}"
    log "Updated tree: $TO_DESC"

    # --- Restart + health check --------------------------------------------------------
    if [[ "$NO_RESTART" == "1" ]]; then
        log "Skipping service restart (--no-restart)."
        write_status complete
        log "Update complete: $FROM_DESC -> $TO_DESC (service NOT restarted)"
        prune_logs
        return 0
    fi

    if [[ "$UNATTENDED" != "1" && "$ASSUME_YES" != "1" ]]; then
        read -r -p "Restart $SERVICE now? [Y/n]: " yn
        if [[ "$yn" =~ ^[Nn] ]]; then
            write_status complete
            log "Update complete: $FROM_DESC -> $TO_DESC (restart skipped by user)"
            prune_logs
            return 0
        fi
    fi

    write_status restarting
    log "Restarting $SERVICE..."
    sudo -n systemctl restart "$SERVICE"

    write_status health_check
    log "Health check..."
    local i active=0
    for i in $(seq 1 10); do
        sleep 3
        if systemctl is-active --quiet "$SERVICE"; then active=1; break; fi
    done
    if [[ "$active" != "1" ]]; then
        log "ERROR: $SERVICE not active 30s after restart"
        false  # -> ERR trap -> rollback
    fi
    # Crash-loop detection: Restart=always makes is-active flap back to
    # active, but each crash increments NRestarts.
    local base_restarts now_restarts
    base_restarts="$(systemctl show -p NRestarts --value "$SERVICE")"
    for i in $(seq 1 10); do
        sleep 3
        now_restarts="$(systemctl show -p NRestarts --value "$SERVICE")"
        if ! systemctl is-active --quiet "$SERVICE" || [[ "$now_restarts" != "$base_restarts" ]]; then
            log "ERROR: $SERVICE unstable after restart (crash loop detected)"
            false  # -> ERR trap -> rollback
        fi
    done

    write_status complete
    log "Update complete: $FROM_DESC -> $TO_DESC"
    prune_logs
}

# Stash-pop conflict policy. During stash pop: ours = new upstream HEAD,
# theirs = the stash (field config).
resolve_stash_conflicts() {
    log "Stash pop reported conflicts — applying resolution policy..."
    local line code p
    while IFS= read -r line; do
        code="${line:0:2}"
        p="${line:3}"
        case "$code" in
            UU)
                if [[ "$p" == config/*.ini || "$p" == config/active_config.txt ]]; then
                    log "  keep field values: $p"
                    git checkout --theirs -- "$p"
                    git add -- "$p"
                else
                    log "  unresolvable content conflict: $p"
                    false  # -> ERR trap -> rollback
                fi
                ;;
            UD)
                # Stash recorded deletion, upstream added/modified => new upstream file.
                log "  keep upstream file: $p"
                git checkout --ours -- "$p"
                git add -- "$p"
                ;;
            DU|AA|AU|UA)
                log "  unresolvable conflict ($code): $p"
                false  # -> ERR trap -> rollback
                ;;
        esac
    done < <(git status --porcelain=v1 | grep -E '^(UU|UD|DU|AA|AU|UA) ' || true)
}

prune_logs() {
    ls -1 "$LOG_DIR"/owl_update_*.log 2>/dev/null | sort | head -n -10 | xargs -r rm -f
}

main "$@"
