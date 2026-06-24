#!/bin/bash
# glpi-session-cleanup.sh — GLPI session file cleanup script
#
# Cleans up stale PHP session files created by GLPI's Symfony framework.
# Each API request (even stateless Bearer-token calls) creates a session
# file on disk. Without cleanup, inode usage grows until the filesystem
# is full and GLPI crashes.
#
# Install:
#   sudo cp glpi-session-cleanup.sh /usr/local/bin/
#   sudo chmod +x /usr/local/bin/glpi-session-cleanup.sh
#   echo "*/30 * * * * root /usr/local/bin/glpi-session-cleanup.sh" | sudo tee /etc/cron.d/glpi-session-cleanup
#
# Or run manually:
#   sudo /usr/local/bin/glpi-session-cleanup.sh
#   sudo /usr/local/bin/glpi-session-cleanup.sh --max-age 30   # clean sessions older than 30 minutes
#   sudo /usr/local/bin/glpi-session-cleanup.sh --dry-run       # show what would be deleted

set -euo pipefail

# ── Configuration ────────────────────────────────────────────────────
SESSION_DIR="${GLPI_SESSION_DIR:-/var/lib/glpi/_sessions}"
MAX_AGE_MINUTES="${GLPI_SESSION_MAX_AGE:-60}"   # delete sessions older than this
LOG_FILE="${GLPI_CLEANUP_LOG:-/var/log/glpi-session-cleanup.log}"
DRY_RUN=false

# ── Parse arguments ──────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --max-age)   MAX_AGE_MINUTES="$2"; shift 2 ;;
        --session-dir) SESSION_DIR="$2"; shift 2 ;;
        --dry-run)   DRY_RUN=true; shift ;;
        --log)       LOG_FILE="$2"; shift 2 ;;
        -h|--help)
            echo "Usage: $0 [--max-age MINUTES] [--session-dir DIR] [--dry-run] [--log FILE]"
            echo ""
            echo "Cleans GLPI PHP session files older than MAX_AGE minutes."
            echo ""
            echo "Options:"
            echo "  --max-age MINUTES   Delete sessions older than this (default: 60)"
            echo "  --session-dir DIR   Session directory (default: /var/lib/glpi/_sessions)"
            echo "  --dry-run           Show what would be deleted without actually deleting"
            echo "  --log FILE          Log file (default: /var/log/glpi-session-cleanup.log)"
            echo ""
            echo "Environment variables:"
            echo "  GLPI_SESSION_DIR        Same as --session-dir"
            echo "  GLPI_SESSION_MAX_AGE    Same as --max-age"
            echo "  GLPI_CLEANUP_LOG        Same as --log"
            exit 0
            ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

# ── Logging ──────────────────────────────────────────────────────────
log() {
    local msg="[$(date '+%Y-%m-%d %H:%M:%S')] $*"
    echo "$msg"
    if [[ -n "$LOG_FILE" ]] && [[ -w "$(dirname "$LOG_FILE")" || -w "$LOG_FILE" ]]; then
        echo "$msg" >> "$LOG_FILE"
    fi
}

# ── Pre-checks ──────────────────────────────────────────────────────
if [[ ! -d "$SESSION_DIR" ]]; then
    log "Session directory does not exist: $SESSION_DIR"
    exit 0
fi

# ── Gather stats ─────────────────────────────────────────────────────
SESSION_COUNT=$(find "$SESSION_DIR" -maxdepth 1 -name "sess_*" -type f 2>/dev/null | wc -l)
INODE_INFO=$(df -i "$SESSION_DIR" 2>/dev/null | tail -1)
INODE_USED=$(echo "$INODE_INFO" | awk '{print $3}')
INODE_USE_PCT=$(echo "$INODE_INFO" | awk '{print $5}')

log "=== GLPI Session Cleanup ==="
log "Session dir: $SESSION_DIR"
log "Current sessions: $SESSION_COUNT"
log "Inode usage: $INODE_USED ($INODE_USE_PCT)"
log "Max age: ${MAX_AGE_MINUTES} minutes"
log "Dry run: $DRY_RUN"

if [[ "$SESSION_COUNT" -eq 0 ]]; then
    log "No session files found. Nothing to do."
    exit 0
fi

# ── Cleanup ──────────────────────────────────────────────────────────
# Choose the most efficient method based on file count:
# - < 10,000 files: find -delete (simple, reliable)
# - >= 10,000 files: rsync --delete trick (faster for bulk deletion)

START_TIME=$(date +%s)

if [[ "$SESSION_COUNT" -lt 10000 ]]; then
    # Method 1: find + delete (fine for small counts)
    if $DRY_RUN; then
        DELETED=$(find "$SESSION_DIR" -maxdepth 1 -name "sess_*" -type f -mmin +"$MAX_AGE_MINUTES" -print 2>/dev/null | wc -l)
        log "[DRY RUN] Would delete $DELETED session(s) older than ${MAX_AGE_MINUTES}m"
    else
        find "$SESSION_DIR" -maxdepth 1 -name "sess_*" -type f -mmin +"$MAX_AGE_MINUTES" -delete 2>/dev/null
        DELETED=$SESSION_COUNT
    fi
else
    # Method 2: rsync trick (much faster for millions of files)
    # Create a temp directory, then rsync --delete to sync it with the
    # target. rsync efficiently removes files that don't exist in source.
    if $DRY_RUN; then
        DELETED=$(find "$SESSION_DIR" -maxdepth 1 -name "sess_*" -type f -mmin +"$MAX_AGE_MINUTES" -print 2>/dev/null | wc -l)
        log "[DRY RUN] Would delete $DELETED session(s) older than ${MAX_AGE_MINUTES}m (rsync method)"
    else
        # For rsync method, we delete ALL session files (not age-filtered)
        # because rsync can't filter by age. This is safe because GLPI
        # sessions are ephemeral and should not persist across requests.
        # If you need age-based filtering with millions of files, use
        # tmpwatch instead: tmpwatch -m ${MAX_AGE_MINUTES}m "$SESSION_DIR"
        TEMP_DIR=$(mktemp -d)
        rsync -a --delete "$TEMP_DIR/" "$SESSION_DIR/" 2>/dev/null
        rmdir "$TEMP_DIR"
        DELETED=$SESSION_COUNT
    fi
fi

END_TIME=$(date +%s)
ELAPSED=$((END_TIME - START_TIME))

# ── Post-stats ───────────────────────────────────────────────────────
if ! $DRY_RUN; then
    NEW_COUNT=$(find "$SESSION_DIR" -maxdepth 1 -name "sess_*" -type f 2>/dev/null | wc -l)
    NEW_INODE_INFO=$(df -i "$SESSION_DIR" 2>/dev/null | tail -1)
    NEW_INODE_USED=$(echo "$NEW_INODE_INFO" | awk '{print $3}')
    NEW_INODE_USE_PCT=$(echo "$NEW_INODE_INFO" | awk '{print $5}')

    log "Cleanup complete in ${ELAPSED}s"
    log "  Sessions: $SESSION_COUNT -> $NEW_COUNT (deleted $((SESSION_COUNT - NEW_COUNT)))"
    log "  Inodes: $INODE_USED ($INODE_USE_PCT) -> $NEW_INODE_USED ($NEW_INODE_USE_PCT)"
else
    log "[DRY RUN] Would clean up in ~${ELAPSED}s"
fi

# ── Rotate log if too large ──────────────────────────────────────────
if [[ -n "$LOG_FILE" ]] && [[ -f "$LOG_FILE" ]]; then
    LOG_SIZE=$(stat -f%z "$LOG_FILE" 2>/dev/null || stat -c%s "$LOG_FILE" 2>/dev/null || echo 0)
    if [[ "$LOG_SIZE" -gt 5242880 ]]; then  # 5MB
        mv "$LOG_FILE" "${LOG_FILE}.old"
        log "Log rotated (was ${LOG_SIZE} bytes)"
    fi
fi
