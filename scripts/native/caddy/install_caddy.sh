#!/usr/bin/env bash
# Install the Caddy release this site is written for and tested against
# (CADDY_VERSION, beside this file), from Caddy's own apt repository — the
# stock build, no plugins. Not Ubuntu's package, which is years older and does
# not read this Caddyfile. Used as it stands by CI and, later, for the first
# install on the VM.
#
# The package is not held at this release. Whether the VM then updates it by
# itself is a separate question that check_caddy_updates.sh answers; README.md,
# "Caddy updates", has what to do when the version moves.
#
# The package starts a placeholder site as soon as it is installed; this stops
# and disables the service again, so nothing is served until install.sh has put
# the real configuration in place and the owner starts it.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERSION="$(tr -d '[:space:]' < "$HERE/CADDY_VERSION")"

# A Caddy that is already here is left exactly as it is: not reinstalled, not
# stopped, and never downgraded or upgraded to make the versions agree.
if command -v caddy >/dev/null 2>&1; then
  FOUND="$(caddy version | cut -d' ' -f1)"
  if [ "$FOUND" = "v$VERSION" ]; then
    echo "Caddy $FOUND is already installed; nothing to do"
    exit 0
  fi
  echo "STOPPED: Caddy $FOUND is installed and v$VERSION is the reviewed version."
  echo "Nothing was changed. See README.md, \"If Caddy is not the reviewed version\"."
  exit 1
fi

sudo apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl gnupg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' |
  sudo gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' |
  sudo tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
sudo apt-get update
sudo apt-get install -y "caddy=$VERSION"
sudo systemctl disable --now caddy

FOUND="$(caddy version | cut -d' ' -f1)"
if [ "$FOUND" != "v$VERSION" ]; then
  echo "expected Caddy v$VERSION, found $FOUND"
  exit 1
fi
echo "Caddy $FOUND installed, stopped and disabled"
