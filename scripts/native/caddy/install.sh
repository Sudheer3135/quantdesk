#!/usr/bin/env bash
# Put QuantDesk's public site in place on the Azure VM: the compiled
# dashboard, the Caddyfile, and Caddy's service settings. Run on the VM, after
# the market has closed, as the desk's user (it uses sudo for the system
# paths). Safe to run again.
#
# Two parts, in this order and never mixed:
#
#   1. Check. Everything is prepared and validated in a temporary directory.
#      Nothing that is being served, and no file Caddy runs on, is touched.
#      Any failure stops here and leaves the machine as it was.
#   2. Promote. Only when every check has passed are the prepared files moved
#      into place, each one beside a `.previous` copy of what it replaced. If a
#      move fails, the ones already made are undone.
#
# It does not start, restart, enable or reload Caddy, open a port, change DNS,
# or touch quantdesk-api, PostgreSQL, Redis or the Vite service — README.md in
# this directory has the whole procedure, the recovery and the rollback. Caddy
# itself must already be installed (install_caddy.sh).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"

stop() {
  echo "STOPPED: $*"
  echo "Nothing was changed."
  exit 1
}

# QUANTDESK_INSTALL_ROOT is a test hook. On the VM it is not in the environment
# at all, and the paths below are the real ones. The tests set it to a scratch
# directory, so that this same script can be run without a real system.
#
# Absent and empty are different things here. Absent is production. Present
# must name a scratch directory: an empty value, `/`, or anything that is not
# an existing directory other than the real root is refused outright, before
# anything is read or written — never quietly taken to mean production.
if [ -z "${QUANTDESK_INSTALL_ROOT+set}" ]; then
  SCRATCH=no
  PREFIX=""
else
  SCRATCH=yes
  PREFIX="$QUANTDESK_INSTALL_ROOT"
  [ -n "$PREFIX" ] ||
    stop "QUANTDESK_INSTALL_ROOT is set but empty. For production it must not be set at all" \
         "(unset QUANTDESK_INSTALL_ROOT)."
  case "$PREFIX" in
    /*) ;;
    *) stop "QUANTDESK_INSTALL_ROOT must be an absolute path to a scratch directory." ;;
  esac
  [ -d "$PREFIX" ] || stop "QUANTDESK_INSTALL_ROOT does not name an existing directory."
  PREFIX="$(cd "$PREFIX" && pwd -P)" || stop "QUANTDESK_INSTALL_ROOT could not be resolved."
  # Compared as a place, not as a spelling: `//` is the root too.
  ! [ "$PREFIX" -ef / ] ||
    stop "QUANTDESK_INSTALL_ROOT is the real root of this machine. For production it must not" \
         "be set at all (unset QUANTDESK_INSTALL_ROOT)."
  echo "installing under $PREFIX"
fi
ENV_FILE="$PREFIX/etc/quantdesk/caddy.env"
WEB_ROOT="$PREFIX/srv/quantdesk/web"
CADDYFILE="$PREFIX/etc/caddy/Caddyfile"
DROPIN="$PREFIX/etc/systemd/system/caddy.service.d/quantdesk.conf"

# ---- 1. check ---------------------------------------------------------------

# The first gate, before anything else is even read. Everything here was
# reviewed and tested against exactly one Caddy release. Any other — older or
# newer — stops here; README.md, "If Caddy is not the reviewed version", has
# what to do. Nothing is downgraded, upgraded or stopped by this script.
echo "caddy      ..."
command -v caddy >/dev/null 2>&1 || stop "caddy is not installed — run install_caddy.sh"
WANT="v$(tr -d '[:space:]' < "$HERE/CADDY_VERSION")"
HAVE="$(caddy version | cut -d' ' -f1)"
[ "$HAVE" = "$WANT" ] ||
  stop "Caddy $HAVE is installed and $WANT is the reviewed version." \
       "See README.md, \"If Caddy is not the reviewed version\"."
echo "           $HAVE"

# The settings file. The one thing created before the checks are complete is
# this file, the first time, from the template: it replaces nothing, nothing
# reads it yet, and the run ends there so that it can be filled in.
echo "settings   ..."
if ! sudo test -f "$ENV_FILE"; then
  sudo install -d -o root -g root -m 700 "$PREFIX/etc/quantdesk"
  sudo install -o root -g root -m 600 "$HERE/caddy.env.example" "$ENV_FILE"
  # The API key goes from one root/owner-only file to another, unseen.
  KEY_LINE="$(sed -n 's/^API_KEY=//p' "$ROOT/.env" | tail -1)"
  if [ -n "$KEY_LINE" ]; then
    printf 'QUANTDESK_API_KEY=%s\n' "$KEY_LINE" |
      sudo sh -c "grep -v '^QUANTDESK_API_KEY=' '$ENV_FILE' > '$ENV_FILE.tmp'; cat >> '$ENV_FILE.tmp'; chmod 600 '$ENV_FILE.tmp'; mv '$ENV_FILE.tmp' '$ENV_FILE'"
  fi
  unset KEY_LINE
  echo "           created $ENV_FILE — set QUANTDESK_HOSTNAME, QUANTDESK_BASIC_USER and"
  echo "           QUANTDESK_BASIC_HASH in it (README.md, \"Going live\"), then run this again"
  exit 0
fi

# Held in memory for the checks below and never printed; a failure names the
# setting, not its value.
SETTINGS="$(sudo cat "$ENV_FILE")"
setting() { printf '%s\n' "$SETTINGS" | sed -n "s/^$1=//p"; }
while IFS= read -r line; do
  case "$line" in ""|\#*) continue ;; esac
  name="${line%%=*}"
  case "$name" in
    QUANTDESK_HOSTNAME|QUANTDESK_BASIC_USER|QUANTDESK_BASIC_HASH|QUANTDESK_API_KEY|QUANTDESK_CERTIFICATES) ;;
    *) stop "$ENV_FILE sets something this site does not take: ${name:0:40}" ;;
  esac
  [ "$(printf '%s\n' "$SETTINGS" | grep -c "^$name=")" = 1 ] || stop "$name is set more than once"
done <<< "$SETTINGS"
case "$SETTINGS" in *REPLACE_WITH*) stop "$ENV_FILE still holds placeholders — fill them in" ;; esac
# A public DNS name: dotted, and not an address. Caddy would quietly give
# `localhost` or an IP its own internal certificate, which no browser trusts.
printf '%s' "$(setting QUANTDESK_HOSTNAME)" |
  grep -qE '^([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)+[a-z]([a-z0-9-]*[a-z0-9])?$' ||
  stop "QUANTDESK_HOSTNAME is not a public DNS name (README.md, \"Hostname and certificate\")"
printf '%s' "$(setting QUANTDESK_BASIC_USER)" | grep -qE '^[A-Za-z0-9._-]+$' ||
  stop "QUANTDESK_BASIC_USER is missing or has characters a sign-in name cannot"
# bcrypt at cost 14, as README.md's command produces: the prefix that says
# so, and the 53 characters of salt and hash that follow it.
printf '%s' "$(setting QUANTDESK_BASIC_HASH)" | grep -qE '^\$2[aby]\$14\$[./A-Za-z0-9]{53}$' ||
  stop "QUANTDESK_BASIC_HASH is not a bcrypt hash of cost 14 — see README.md"
[ -n "$(setting QUANTDESK_API_KEY)" ] || stop "QUANTDESK_API_KEY is missing"
case "$(setting QUANTDESK_CERTIFICATES)" in
  ""|production|staging) ;;
  *) stop "QUANTDESK_CERTIFICATES may only be staging (or absent, for production)" ;;
esac
echo "           $ENV_FILE"

STAGE="$(mktemp -d)"
trap 'rm -rf "${STAGE:?}"' EXIT

echo "dashboard  ..."
"$ROOT/scripts/native/build_web.sh" "$STAGE/web" || stop "the dashboard did not build"
[ -s "$STAGE/web/index.html" ] || stop "the dashboard build has no index.html"
echo "           built"

echo "caddyfile  ..."
cp "$HERE/Caddyfile" "$STAGE/Caddyfile"
cp "$HERE/caddy.service.d/quantdesk.conf" "$STAGE/quantdesk.conf"
# With the real settings, read line by line and exported as they stand, the
# way systemd reads them: sourcing would expand the `$` in the hash. Loading a
# configuration to check it creates its log file and storage directory, so for
# the check alone those are pointed into the temporary directory.
(
  while IFS= read -r line; do
    case "$line" in ""|\#*) ;; *) export "$line" ;; esac
  done <<< "$SETTINGS"
  export QUANTDESK_ACCESS_LOG="$STAGE/check/access.log" QUANTDESK_STORAGE_ROOT="$STAGE/check/storage"
  export QUANTDESK_WEB_ROOT="$STAGE/web" HOME="$STAGE/check" XDG_DATA_HOME="$STAGE/check/data"
  export XDG_CONFIG_HOME="$STAGE/check/config"
  caddy validate --adapter caddyfile --config "$STAGE/Caddyfile" >/dev/null 2>&1
) || stop "Caddy does not accept the Caddyfile with these settings (caddy validate)"
echo "           valid"

# The certificate authority it would really use, and where it would keep what
# it obtains — read from the adapted configuration, with nothing overridden.
echo "authority  ..."
"$HERE/certificate_authority.sh" --env-file /dev/stdin "$STAGE/Caddyfile" \
  <<< "$SETTINGS" > "$STAGE/authority" 2>&1 || {
  sed 's/^/           /' "$STAGE/authority"
  stop "the certificate authority or its storage is not one of the two allowed"
}
sed 's/^/           /' "$STAGE/authority"

# What the files will land in and who will read them.
echo "machine    ..."
id caddy >/dev/null 2>&1 || stop "there is no user \`caddy\` — was the package installed?"
for needed in "$PREFIX/etc/caddy" "$PREFIX/etc/systemd/system" "$PREFIX/var/lib/caddy"; do
  sudo test -d "$needed" || stop "$needed does not exist"
done
grep -q '^EnvironmentFile=/etc/quantdesk/caddy.env$' "$STAGE/quantdesk.conf" ||
  stop "the service drop-in does not read /etc/quantdesk/caddy.env"
echo "           ready"

# ---- 2. promote -------------------------------------------------------------

# Laid down beside their destinations first, under `.new` names nothing reads.
echo "promoting  ..."
sudo install -d -o root -g root -m 755 "$PREFIX/srv/quantdesk" "$(dirname "$DROPIN")"
sudo rm -rf "${WEB_ROOT:?}.new" "${CADDYFILE:?}.new" "${DROPIN:?}.new"
# Owned by root and world-readable: Caddy (user `caddy`) only ever reads it.
sudo cp -R "$STAGE/web" "$WEB_ROOT.new"
sudo chown -R root:root "$WEB_ROOT.new"
sudo chmod -R u=rwX,go=rX "$WEB_ROOT.new"
sudo install -o root -g root -m 644 "$STAGE/Caddyfile" "$CADDYFILE.new"
sudo install -o root -g root -m 644 "$STAGE/quantdesk.conf" "$DROPIN.new"
# The package's own Caddyfile, kept once, for removing the site altogether.
if sudo test -f "$CADDYFILE" && ! sudo test -f "$CADDYFILE.before-quantdesk"; then
  sudo cp -p "$CADDYFILE" "$CADDYFILE.before-quantdesk"
fi

# Then swapped in, one rename each way: what is there becomes `.previous`,
# `.new` takes its place. MOVED_ASIDE and PLACED remember exactly how far that
# got, so that undoing it puts back only what this run itself moved.
MOVED_ASIDE=() PLACED=()
swap() {
  if sudo test -e "$1"; then
    sudo rm -rf "${1:?}.previous" || return 1
    sudo mv "$1" "$1.previous" || return 1
    MOVED_ASIDE+=("$1")
  fi
  sudo mv "$1.new" "$1" || return 1
  PLACED+=("$1")
}
undo() {
  local target restored=0
  for target in ${PLACED[@]+"${PLACED[@]}"}; do
    sudo rm -rf "${target:?}" || restored=1
  done
  for target in ${MOVED_ASIDE[@]+"${MOVED_ASIDE[@]}"}; do
    sudo mv "$target.previous" "$target" || restored=1
  done
  sudo rm -rf "${WEB_ROOT:?}.new" "${CADDYFILE:?}.new" "${DROPIN:?}.new" || restored=1
  return "$restored"
}
for target in "$WEB_ROOT" "$CADDYFILE" "$DROPIN"; do
  if ! swap "$target"; then
    echo "FAILED while putting $target in place; restoring what was there"
    if undo; then
      echo "Restored. The site is as it was before this run."
    else
      echo "NOT FULLY RESTORED. See README.md, \"If promotion fails\", before doing anything else."
    fi
    exit 1
  fi
done
# So that systemd has read the drop-in by the time Caddy is next started. It
# starts and restarts nothing. With an install root set this is a scratch run
# and systemd is not addressed at all — decided by that variable alone.
if [ "$SCRATCH" = no ]; then
  sudo systemctl daemon-reload
else
  echo "           systemctl daemon-reload SKIPPED: QUANTDESK_INSTALL_ROOT is set (scratch/test run)"
fi
echo "           $WEB_ROOT"
echo "           $CADDYFILE"
echo "           $DROPIN"
echo "           what they replaced is beside each, as .previous"
echo
if [ "$(setting QUANTDESK_CERTIFICATES)" = staging ]; then
  echo "STAGING TRIAL: this configuration obtains a certificate browsers do not trust."
  echo "It is for the trial only (README.md, \"Hostname and certificate\")."
fi
echo "Caddy was not started, restarted or reloaded. Next: README.md, \"Going live\"."
