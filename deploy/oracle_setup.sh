#!/usr/bin/env bash
# Connect PartsBridge PK to its Oracle Autonomous Database knowledge base (app/kb.py).
#
#   1. Put the wallet in /etc/partsbridge/oracle-wallet (root:partsbridge, 750 / files 640).
#   2. sudo deploy/oracle_setup.sh          (run from the cloned repo on the server)
#
# Asks (hidden input, never echoed, never written to shell history):
#   * the ADMIN password, used once to create a dedicated least-privilege user; it is NOT stored;
#   * the wallet password (the wallet's private key is encrypted with it).
# Creates/refreshes database user PARTSBRIDGE_APP with a random password, stores only that user's credentials in
# /etc/partsbridge/env (root:partsbridge 640), creates the tables, downloads the local embedding model, restarts
# the app. Safe to re-run (rotates the app user's password).
set -euo pipefail

ETC=/etc/partsbridge
WALLET=${WALLET:-$ETC/oracle-wallet}
ENV_FILE=$ETC/env
BASE=/opt/partsbridge
VENV=$BASE/venv
APP=$BASE/app
APP_USER=${APP_USER:-PARTSBRIDGE_APP}
MODEL_DIR=$BASE/.cache/fastembed

[ "$(id -u)" = 0 ] || { echo "Run with sudo."; exit 1; }
[ -f "$WALLET/tnsnames.ora" ] || { echo "No wallet in $WALLET"; exit 1; }
DSN=${DSN:-$(grep -oE '^[a-z0-9_]+_tp\b' "$WALLET/tnsnames.ora" | head -1)}
[ -n "$DSN" ] || DSN=$(grep -oE '^[a-z0-9_]+_low\b' "$WALLET/tnsnames.ora" | head -1)
echo "Database service: $DSN"

echo "Installing database driver + embedding runtime into the app venv"
sudo -u partsbridge "$VENV/bin/pip" install -q "oracledb>=2.4" "fastembed>=0.4"

read -rsp "ADMIN password of the Autonomous Database (used once, not stored): " ADMIN_PW; echo
read -rsp "Wallet password (chosen when the wallet was downloaded): " WALLET_PW; echo
APP_PW=$("$VENV/bin/python" -c "import secrets,string;a=string.ascii_letters+string.digits
while True:
    p=''.join(secrets.choice(a) for _ in range(28))
    if any(c.isupper() for c in p) and any(c.islower() for c in p) and sum(c.isdigit() for c in p)>=2: print(p);break")
export ADMIN_PW WALLET_PW APP_PW APP_USER DSN WALLET

echo "Creating / refreshing database user $APP_USER"
"$VENV/bin/python" - <<'PY'
import os, oracledb
w = os.environ["WALLET"]
c = oracledb.connect(user="ADMIN", password=os.environ["ADMIN_PW"], dsn=os.environ["DSN"], config_dir=w,
                     wallet_location=w, wallet_password=os.environ["WALLET_PW"])
cur, u, pw = c.cursor(), os.environ["APP_USER"].upper(), os.environ["APP_PW"]
assert u.isidentifier() and pw.isalnum()           # both are inlined into DDL below
cur.execute("SELECT COUNT(*) FROM dba_users WHERE username = :u", u=u)
if cur.fetchone()[0]:
    cur.execute(f'ALTER USER {u} IDENTIFIED BY "{pw}" ACCOUNT UNLOCK')
    print("  updated password for", u)
else:
    cur.execute(f'CREATE USER {u} IDENTIFIED BY "{pw}" DEFAULT TABLESPACE DATA QUOTA 2G ON DATA')
    print("  created", u)
cur.execute(f"GRANT CREATE SESSION, CREATE TABLE TO {u}")
print("  granted CREATE SESSION, CREATE TABLE (nothing else)")
PY
unset ADMIN_PW

echo "Storing the app user's credentials in $ENV_FILE"
q() { local v=${1//\\/\\\\}; printf '"%s"' "${v//\"/\\\"}"; }    # systemd EnvironmentFile quoting
TMP=$(mktemp); chmod 600 "$TMP"
grep -vE '^(ORACLE_DB_USER|ORACLE_DB_PASSWORD|ORACLE_DB_DSN|ORACLE_WALLET_DIR|ORACLE_WALLET_PASSWORD|KB_MODEL_DIR|KB_DISABLED)=' \
  "$ENV_FILE" > "$TMP" || true
{
  echo ""
  echo "# --- Oracle knowledge base (deploy/oracle_setup.sh) ---"
  echo "ORACLE_DB_USER=$APP_USER"
  echo "ORACLE_DB_PASSWORD=$(q "$APP_PW")"
  echo "ORACLE_DB_DSN=$DSN"
  echo "ORACLE_WALLET_DIR=$WALLET"
  echo "ORACLE_WALLET_PASSWORD=$(q "$WALLET_PW")"
  echo "KB_MODEL_DIR=$MODEL_DIR"
} >> "$TMP"
install -o root -g partsbridge -m 640 "$TMP" "$ENV_FILE"
rm -f "$TMP"
unset WALLET_PW APP_PW
sudo -u partsbridge mkdir -p "$MODEL_DIR"

run_as_app() {   # same environment parser and user as the real service
  systemd-run --quiet --wait --pipe --collect --uid=partsbridge -p EnvironmentFile="$ENV_FILE" \
    -p WorkingDirectory="$APP" "$VENV/bin/python" -m app.kb "$@"
}
echo "Creating tables";               run_as_app init
echo "Downloading embedding model";   run_as_app warm-model
echo "Connection check";              run_as_app ping
systemctl restart partsbridge-app
echo "Done. The app now uses the knowledge base (see 'knowledge_base' in the web research stats)."
