#!/usr/bin/env bash
# ==============================================================================
# E-Cell Exam Portal - Automated Live Backup Script
# Run via crontab:
# */5 * * * * /var/www/ecell-portal/deploy/backup_cron.sh >> /var/log/ecell_backup.log 2>&1
# ==============================================================================

set -euo pipefail

APP_DIR="/var/www/ecell-portal"
BACKUP_DIR="/var/backups/ecell"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
BACKUP_FILE="${BACKUP_DIR}/ecell_backup_${TIMESTAMP}.db"

mkdir -p "${BACKUP_DIR}"

# Run atomic online SQLite backup (zero-downtime, non-blocking)
cd "${APP_DIR}"
python3 server.py backup "${BACKUP_FILE}"

# Keep last 7 days of snapshots, purge older ones to save disk space
find "${BACKUP_DIR}" -name "ecell_backup_*.db" -type f -mtime +7 -delete

echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] Backup successful: ${BACKUP_FILE}"
