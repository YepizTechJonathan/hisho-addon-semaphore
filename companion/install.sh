#!/usr/bin/env bash
# Install in Hisho (src/hisho/companion.py runs this on the Core machine): Semaphore UI with PostgreSQL in rootless
# Docker, on the tailnet at https://<core>:4443. Hisho's own install does the work (pinned images, the owner's time
# zone for schedules). Afterwards: `bash ~/code/hisho/deploy/semaphore/install.sh --save-secrets` stores the admin
# password and the add-on's API token in your secrets store.
set -euo pipefail
HISHO_REPO="${HISHO_REPO:-$HOME/code/hisho}"
exec bash "$HISHO_REPO/deploy/semaphore/install.sh"
