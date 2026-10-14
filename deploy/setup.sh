#!/usr/bin/env bash
# ==============================================================================
# E-Cell Exam Portal - Automated Ubuntu/Debian 1-Click Server Setup
# Usage: sudo bash setup.sh <domain_name>
# Example: sudo bash setup.sh exam.yourcollegeecell.com
# ==============================================================================

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
   echo "[-] This script must be run as root (use sudo)."
   exit 1
fi

DOMAIN="${1:-exam.ecell.local}"
APP_DIR="/var/www/ecell-portal"

echo "[+] Starting automated deployment for domain: ${DOMAIN}..."

# 1. Install prerequisites
echo "[+] Updating apt repositories and installing packages..."
apt-get update -y
apt-get install -y python3 sqlite3 nginx certbot python3-certbot-nginx curl ufw

# 2. Configure OS file descriptor limits for high concurrency
echo "[+] Configuring system file limits (65535)..."
cat << 'EOF' > /etc/security/limits.d/99-ecell.conf
* soft nofile 65535
* hard nofile 65535
root soft nofile 65535
root hard nofile 65535
EOF
sysctl -w fs.file-max=2097152 >/dev/null

# 3. Setup application directory and permissions
echo "[+] Setting up app directory at ${APP_DIR}..."
mkdir -p "${APP_DIR}/data"
mkdir -p "/var/backups/ecell"
mkdir -p "/var/log"

# Set permissions
chown -R www-data:www-data "${APP_DIR}"
chmod -R 750 "${APP_DIR}"
chmod -R 770 "${APP_DIR}/data"

# 4. Install systemd service
echo "[+] Configuring systemd service..."
cp "${APP_DIR}/deploy/ecell.service" /etc/systemd/system/ecell.service
systemctl daemon-reload
systemctl enable ecell.service
systemctl restart ecell.service

# 5. Configure Nginx
echo "[+] Configuring Nginx reverse proxy..."
sed "s/exam.yourcollegeecell.com/${DOMAIN}/g" "${APP_DIR}/deploy/ecell.nginx.conf" > /etc/nginx/sites-available/ecell.conf
ln -sf /etc/nginx/sites-available/ecell.conf /etc/nginx/sites-enabled/ecell.conf
rm -f /etc/nginx/sites-enabled/default

# Verify Nginx configuration
nginx -t
systemctl reload nginx

# 6. Configure automated backup cron job
echo "[+] Setting up 5-minute database backup cron job..."
CRON_JOB="*/5 * * * * ${APP_DIR}/deploy/backup_cron.sh >> /var/log/ecell_backup.log 2>&1"
(crontab -l 2>/dev/null | grep -Fv "backup_cron.sh"; echo "$CRON_JOB") | crontab -

# 7. Configure firewall
echo "[+] Configuring UFW firewall..."
ufw allow OpenSSH || true
ufw allow 'Nginx Full' || true

echo "=========================================================================="
echo "[✓] Deployment Completed Successfully!"
echo "=========================================================================="
echo "Next Steps:"
echo "1. Point your DNS A-Record for '${DOMAIN}' to this server's public IP."
echo "2. Run Certbot to generate your free SSL certificate:"
echo "   sudo certbot --nginx -d ${DOMAIN}"
echo "3. Create your first administrator account:"
echo "   cd ${APP_DIR} && sudo -u www-data python3 server.py create-admin admin@example.com"
echo "4. Check service status:"
echo "   sudo systemctl status ecell"
echo "=========================================================================="
