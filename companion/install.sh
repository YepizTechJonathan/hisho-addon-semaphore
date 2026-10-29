#!/usr/bin/env bash
# Install in Hisho (src/hisho/companion.py runs this on the Core machine): Semaphore UI with PostgreSQL in rootless
# Docker, on the tailnet at https://<core>:4443. Hisho's own install does the work (pinned images, the owner's time
# zone for schedules). Afterwards: `bash ~/code/hisho/deploy/semaphore/install.sh --save-secrets` stores the admin
# password and the add-on's API token in your secrets store.
# First this add-on's playbooks (ansible/) go to ~/semaphore/hisho-ansible, which Hisho's install mounts read-only
# into the container at /hisho/ansible: the "Hisho playbooks" repository the add-on makes is that local path.
set -euo pipefail
HISHO_REPO="${HISHO_REPO:-$HOME/code/hisho}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${HISHO_COMPANION_DATA_DIR:-$HOME/semaphore}"
DEST="${DEST/#\~/$HOME}/hisho-ansible"
mkdir -p "$DEST"
cp -R "$HERE/ansible/." "$DEST/"
chmod -R u=rwX,go=rX "$DEST"
echo "semaphore: Hisho's playbooks -> $DEST (read-only in the container at /hisho/ansible)"
exec bash "$HISHO_REPO/deploy/semaphore/install.sh"
