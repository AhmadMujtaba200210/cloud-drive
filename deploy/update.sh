#!/usr/bin/env bash
# Pull latest code and redeploy (keeps /etc/partsbridge/env). Run from the cloned repo: sudo deploy/update.sh
set -euo pipefail
cd "$(dirname "$0")/.."
git pull --ff-only
exec "$(pwd)/deploy/install.sh"
