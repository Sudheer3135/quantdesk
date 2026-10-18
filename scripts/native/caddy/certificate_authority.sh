#!/usr/bin/env bash
# Which certificate authority would Caddy actually use, and where would it
# keep what it obtains? Answered from the configuration Caddy builds out of
# the Caddyfile and its settings (`caddy adapt`), not from the Caddyfile's
# text — a line that names one CA does not show there is no second one
# behind it.
#
#   certificate_authority.sh [--env-file FILE] [--require production|staging] [Caddyfile]
#
# Prints every issuer and the storage directory in the effective
# configuration. Succeeds only for one of two pairs:
#
#   Let's Encrypt production  kept in  <storage root>/production
#   Let's Encrypt staging     kept in  <storage root>/staging-trial
#
# Anything else fails — a second issuer, another CA, Caddy's internal CA, no
# issuer named at all (Caddy's default is Let's Encrypt and then ZeroSSL), or
# either CA paired with the other's directory or with Caddy's default one.
# `--require` also fails the other of the two pairs.
#
# Read-only. With --env-file the settings are read as systemd reads them (run
# it with sudo for /etc/quantdesk/caddy.env); without, from the environment.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="" REQUIRE="" CADDYFILE="$HERE/Caddyfile"
while [ $# -gt 0 ]; do
  case "$1" in
    --env-file) ENV_FILE="$2"; shift 2 ;;
    --require) REQUIRE="$2"; shift 2 ;;
    *) CADDYFILE="$1"; shift ;;
  esac
done
case "$REQUIRE" in ""|production|staging) ;; *) echo "--require production|staging"; exit 2 ;; esac

if [ -n "$ENV_FILE" ]; then
  # Line by line, as they stand: sourcing would expand the `$` in the hash.
  while IFS= read -r line; do
    case "$line" in ""|\#*) ;; *) export "$line" ;; esac
  done < "$ENV_FILE"
fi

ADAPTED="$(caddy adapt --adapter caddyfile --config "$CADDYFILE" 2>/dev/null)" ||
  { echo "caddy could not adapt $CADDYFILE"; exit 1; }
printf '%s' "$ADAPTED" | REQUIRE="$REQUIRE" \
  STORAGE_ROOT="${QUANTDESK_STORAGE_ROOT:-/var/lib/caddy/quantdesk}" python3 -c '
import json, os, sys

PAIRS = {
    "production": ("https://acme-v02.api.letsencrypt.org/directory", "production"),
    "staging": ("https://acme-staging-v02.api.letsencrypt.org/directory", "staging-trial"),
}
root = os.environ["STORAGE_ROOT"].rstrip("/")

config = json.load(sys.stdin)
policies = config.get("apps", {}).get("tls", {}).get("automation", {}).get("policies") or []
issuers, unnamed = [], not policies
for policy in policies:
    if not policy.get("issuers"):
        unnamed = True
    for issuer in policy.get("issuers") or []:
        issuers.append((issuer.get("module"), issuer.get("ca")))
storage = config.get("storage") or {}

print("effective issuers:")
for module, ca in issuers:
    print(f"  {module}  {ca}")
if unnamed:
    print("  (a policy names no issuer: Caddy would choose — its default pair of CAs,")
    print("   or its own internal CA for a name that is not a public one)")
print("effective certificate storage:")
module, kept_in = storage.get("module", "(none named: Caddy default)"), storage.get("root", "")
print(f"  {module}  {kept_in}")

found = None
if len(issuers) == 1 and issuers[0][0] == "acme" and not unnamed:
    found = next((name for name, (ca, _) in PAIRS.items() if ca == issuers[0][1]), None)
if found is None:
    sys.exit("REFUSED: not exactly one issuer that is Let\x27s Encrypt")
kept = f"{root}/{PAIRS[found][1]}"
if storage.get("module") != "file_system" or storage.get("root") != kept:
    sys.exit(f"REFUSED: Let\x27s Encrypt {found} must be kept in {kept} and nowhere else")
want = os.environ["REQUIRE"]
if want and want != found:
    sys.exit(f"REFUSED: Let\x27s Encrypt {found}, and {want} is required")
print("Let\x27s Encrypt production only" if found == "production" else
      "Let\x27s Encrypt STAGING only — a trial; browsers do not trust its certificates")
'
