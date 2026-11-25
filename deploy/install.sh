#!/usr/bin/env bash
# PartsBridge PK - one-shot installer for an Oracle Cloud VM (Ubuntu 22.04/24.04 or Oracle Linux 8/9; x86_64 or ARM Ampere).
#
#   git clone -b claude/global-car-parts-marketplace-1deni7 https://github.com/AhmadMujtaba200210/cloud-drive.git
#   cd cloud-drive && sudo DOMAIN=parts.example.com EMAIL=you@example.com deploy/install.sh
#   (DOMAIN/EMAIL optional: without them the site is served on http://<public-ip>/)
#
# Installs: the app (uvicorn, 127.0.0.1:8000), SearXNG (127.0.0.1:8888), Firecrawl Local (127.0.0.1:3002),
# nginx on :80/:443 in front, systemd units, OS firewall rules, and a TLS cert when DOMAIN+EMAIL are given.
# Only nginx is public. Safe to re-run (updates code and restarts services).
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }
SRC="$(cd "$(dirname "$0")/.." && pwd)"
SVC_USER=partsbridge
BASE=/opt/partsbridge
APP=$BASE/app
SX=$BASE/searxng
ETC=/etc/partsbridge
DOMAIN="${DOMAIN:-}"
EMAIL="${EMAIL:-}"
log() { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }

# ---------- 1. packages ----------
log "Installing system packages"
if command -v apt-get >/dev/null; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y
  apt-get install -y python3 python3-venv python3-dev git curl nginx build-essential pkg-config libssl-dev \
                     libxml2-dev libxslt1-dev zlib1g-dev rsync
  apt-get install -y chromium-browser 2>/dev/null || apt-get install -y chromium 2>/dev/null || true
  [ -n "$DOMAIN" ] && apt-get install -y certbot python3-certbot-nginx
  PKG=apt
elif command -v dnf >/dev/null; then
  dnf install -y python3.11 python3.11-devel git curl nginx gcc gcc-c++ make pkgconf-pkg-config openssl-devel \
                 libxml2-devel libxslt-devel zlib-devel rsync policycoreutils-python-utils || \
  dnf install -y python3 python3-devel git curl nginx gcc gcc-c++ make openssl-devel libxml2-devel libxslt-devel zlib-devel rsync
  dnf install -y chromium 2>/dev/null || true
  [ -n "$DOMAIN" ] && { dnf install -y epel-release 2>/dev/null || dnf install -y oracle-epel-release-el9 2>/dev/null || true; dnf install -y certbot python3-certbot-nginx || true; }
  PKG=dnf
else
  echo "Unsupported OS (need apt or dnf)"; exit 1
fi
PY=$(command -v python3.11 || command -v python3)
"$PY" -c 'import sys; assert sys.version_info >= (3, 10), "Python >= 3.10 required"'

# ---------- 2. service user + code ----------
log "Creating user and copying code to $APP"
id "$SVC_USER" >/dev/null 2>&1 || useradd --system --create-home --home-dir $BASE --shell /usr/sbin/nologin "$SVC_USER"
mkdir -p $APP $ETC
rsync -a --delete --exclude .git --exclude .env --exclude '__pycache__' --exclude '.pytest_cache' "$SRC/" "$APP/"
chown -R $SVC_USER:$SVC_USER $BASE

# ---------- 3. secrets file (never in git) ----------
if [ ! -f $ETC/env ]; then
  log "Creating $ETC/env - EDIT IT to add your LLM keys"
  cp "$APP/.env.example" $ETC/env
  cat >> $ETC/env <<ENV

# --- production defaults ---
DISABLE_DEMO=1
RATE_LIMIT_PER_HOUR=30
# ACCESS_TOKEN=   # set to make the site private: open https://host/?token=VALUE
ENV
  sed -i 's#^SEARXNG_URL=.*#SEARXNG_URL=http://127.0.0.1:8888#; s#^FIRECRAWL_BASE_URL=.*#FIRECRAWL_BASE_URL=http://127.0.0.1:3002#' $ETC/env
fi
chown root:$SVC_USER $ETC/env; chmod 640 $ETC/env

# ---------- 4. python app ----------
log "Python venv for the app"
sudo -u $SVC_USER "$PY" -m venv $BASE/venv
sudo -u $SVC_USER $BASE/venv/bin/pip install -q -U pip
sudo -u $SVC_USER $BASE/venv/bin/pip install -q -r $APP/requirements.txt

# ---------- 5. SearXNG (official git, not the unofficial PyPI name) ----------
log "SearXNG"
if [ ! -d $SX/src ]; then sudo -u $SVC_USER git clone --depth 1 https://github.com/searxng/searxng.git $SX/src;
else sudo -u $SVC_USER git -C $SX/src pull --ff-only || true; fi
sudo -u $SVC_USER "$PY" -m venv $SX/venv
sudo -u $SVC_USER $SX/venv/bin/pip install -q -U pip setuptools wheel
sudo -u $SVC_USER $SX/venv/bin/pip install -q -r $SX/src/requirements.txt
sudo -u $SVC_USER mkdir -p $SX/tmp      # SearXNG keeps its sqlite cache in TMPDIR
[ -f $ETC/searxng_secret ] || { "$PY" -c 'import secrets;print(secrets.token_hex(32))' > $ETC/searxng_secret; }
chown root:$SVC_USER $ETC/searxng_secret; chmod 640 $ETC/searxng_secret

# ---------- 6. Firecrawl Local (Rust; ~10-20 min first build, longer on small ARM VMs) ----------
log "Firecrawl Local"
if [ ! -x $BASE/.cargo/bin/firecrawl-local ]; then
  [ -x $BASE/.cargo/bin/cargo ] || sudo -u $SVC_USER -H bash -c 'curl -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal'
  sudo -u $SVC_USER -H bash -c "$BASE/.cargo/bin/cargo install firecrawl-local"
fi

# ---------- 7. systemd ----------
log "systemd units"
cat > /etc/systemd/system/partsbridge-searxng.service <<UNIT
[Unit]
Description=PartsBridge SearXNG (local metasearch)
After=network-online.target
[Service]
User=$SVC_USER
WorkingDirectory=$SX/src
Environment=SEARXNG_SETTINGS_PATH=$APP/infra/searxng/settings.yml
Environment=TMPDIR=$SX/tmp
PrivateTmp=true
ExecStart=/bin/bash -c 'SEARXNG_SECRET=\$(cat $ETC/searxng_secret) exec $SX/venv/bin/python -m searx.webapp'
Restart=always
NoNewPrivileges=true
[Install]
WantedBy=multi-user.target
UNIT

cat > /etc/systemd/system/partsbridge-firecrawl.service <<UNIT
[Unit]
Description=PartsBridge Firecrawl Local (scraper, cloud off)
After=network-online.target
[Service]
User=$SVC_USER
Environment=FIRECRAWL_CLOUD=off
ExecStart=$BASE/.cargo/bin/firecrawl-local serve --host 127.0.0.1 --port 3002
Restart=always
NoNewPrivileges=true
[Install]
WantedBy=multi-user.target
UNIT

cat > /etc/systemd/system/partsbridge-app.service <<UNIT
[Unit]
Description=PartsBridge PK web app
After=network-online.target partsbridge-searxng.service partsbridge-firecrawl.service
Wants=partsbridge-searxng.service partsbridge-firecrawl.service
[Service]
User=$SVC_USER
WorkingDirectory=$APP
EnvironmentFile=$ETC/env
ExecStart=$BASE/venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --proxy-headers --forwarded-allow-ips 127.0.0.1
Restart=always
NoNewPrivileges=true
PrivateTmp=true
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now partsbridge-searxng partsbridge-firecrawl
systemctl enable partsbridge-app; systemctl restart partsbridge-app

# ---------- 8. nginx ----------
log "nginx"
NGX_CONF=/etc/nginx/conf.d/partsbridge.conf
[ -d /etc/nginx/sites-enabled ] && rm -f /etc/nginx/sites-enabled/default
sed "s/__SERVER_NAME__/${DOMAIN:-_}/" "$APP/deploy/nginx.conf" > $NGX_CONF
[ -s /proc/net/if_inet6 ] || sed -i '/listen \[::\]/d' $NGX_CONF      # host without IPv6
nginx -t
systemctl enable nginx; systemctl restart nginx
if [ "$PKG" = dnf ] && command -v setsebool >/dev/null; then setsebool -P httpd_can_network_connect 1 || true; fi

# ---------- 9. OS firewall (Oracle images block 80/443 by default even after the cloud Security List allows them) ----------
log "Opening ports 80/443 in the OS firewall"
if command -v firewall-cmd >/dev/null && systemctl is-active --quiet firewalld; then
  firewall-cmd --permanent --add-service=http --add-service=https && firewall-cmd --reload
elif command -v iptables >/dev/null; then
  for p in 80 443; do
    iptables -C INPUT -p tcp --dport $p -m state --state NEW -j ACCEPT 2>/dev/null && continue
    # Oracle Ubuntu images end INPUT with a catch-all REJECT: insert just before it (else at the top).
    pos=$(iptables -L INPUT --line-numbers -n | awk '$2=="REJECT"{print $1; exit}')
    iptables -I INPUT "${pos:-1}" -p tcp --dport $p -m state --state NEW -j ACCEPT
  done
  command -v netfilter-persistent >/dev/null && netfilter-persistent save || true
fi

# ---------- 10. TLS ----------
if [ -n "$DOMAIN" ] && [ -n "$EMAIL" ] && command -v certbot >/dev/null; then
  log "TLS certificate for $DOMAIN"
  certbot --nginx -d "$DOMAIN" -m "$EMAIL" --agree-tos --non-interactive --redirect || echo "certbot failed: check DNS A record -> this VM"
fi

# ---------- 11. checks ----------
log "Health checks"
sleep 4
for s in partsbridge-searxng partsbridge-firecrawl partsbridge-app nginx; do printf '%-24s %s\n' "$s" "$(systemctl is-active $s)"; done
curl -sf http://127.0.0.1:8888/healthz >/dev/null && echo "SearXNG   ok" || echo "SearXNG   NOT ready (journalctl -u partsbridge-searxng)"
curl -s -o /dev/null http://127.0.0.1:3002/ && echo "Firecrawl ok" || echo "Firecrawl NOT ready (journalctl -u partsbridge-firecrawl)"
curl -sf http://127.0.0.1/healthz && echo "  <- app via nginx ok" || echo "app via nginx NOT ready (journalctl -u partsbridge-app)"
grep -qE '^LAUNCHSHIELD_(GROQ|OPENROUTER|GEMINI)_API_KEY=.+' $ETC/env || echo "!! No LLM keys yet: sudo nano $ETC/env  then  sudo systemctl restart partsbridge-app"
IP=$(curl -s -m 5 https://ifconfig.me || echo "<public-ip>")
echo; echo "Open: ${DOMAIN:+https://$DOMAIN/}${DOMAIN:-http://$IP/}"
echo "Also allow TCP 80 and 443 in the OCI console: VCN > Subnet > Security List > Ingress rules (source 0.0.0.0/0)."
