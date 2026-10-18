#!/usr/bin/env bash
# Evidence for the question "will this machine update Caddy by itself?" — and
# only evidence. Run on the VM during the deployment review, after
# install_caddy.sh. Read-only: it changes no setting and installs nothing (the
# last step is unattended-upgrades' own dry run).
#
# It does not answer the question. Whether unattended-upgrades would install a
# Caddy update depends on how its origin patterns, with their wildcards and
# substitutions, match the repository; on blacklists, holds and pins; and on
# whether it runs at all. This script does not evaluate that policy and does
# not pretend to: a line that mentions Caddy's origin does not show an update
# would be installed, and the absence of one does not show it would not. It
# prints what a person needs to decide, and says so.
set -uo pipefail

section() { echo; echo "== $*"; }
# Whatever a command prints, and how it ended if it did not end well.
show() {
  local out status
  out="$("$@" 2>&1)"; status=$?
  if [ -n "$out" ]; then printf '%s\n' "$out"; else echo "   (no output)"; fi
  [ "$status" -eq 0 ] || echo "   !! \`$*\` exited $status"
  return 0
}

section "installed and candidate versions, and any pin"
show apt-cache policy caddy

section "the Caddy repository, as apt sees it (origin o=, label l=, archive a=)"
show sh -c "apt-cache policy | grep -B1 -A1 'cloudsmith.io/public/caddy' || echo '   not found — is the repository configured?'"

section "unattended-upgrades: origins it may install from (patterns are not evaluated here)"
show sh -c "apt-config dump | grep -E '^Unattended-Upgrade::(Allowed-Origins|Origins-Pattern)' || echo '   none configured'"

section "unattended-upgrades: packages it is told to leave alone"
show sh -c "apt-config dump | grep -E '^Unattended-Upgrade::Package-(Blacklist|Whitelist)' || echo '   none configured'"

section "packages held back by hand"
show apt-mark showhold

section "does unattended-upgrades run at all"
show sh -c "apt-config dump | grep -E '^APT::Periodic::(Unattended-Upgrade|Update-Package-Lists) ' || echo '   APT::Periodic not set'"
show systemctl is-enabled apt-daily.timer apt-daily-upgrade.timer
show systemctl is-active apt-daily.timer apt-daily-upgrade.timer

section "unattended-upgrades' own dry run (lines naming its origins, Caddy or the repository)"
DRY="$(sudo unattended-upgrade --dry-run --debug 2>&1)"; DRY_STATUS=$?
if [ "$DRY_STATUS" -ne 0 ]; then
  echo "   !! the dry run FAILED (exit $DRY_STATUS). Its output, whole:"
  printf '%s\n' "$DRY" | sed 's/^/   /'
else
  printf '%s\n' "$DRY" | grep -iE 'allowed origins|caddy|cloudsmith' ||
    echo "   the dry run did not mention Caddy (which, with no update pending, says nothing)"
fi

section "verdict"
echo "UNKNOWN — MANUAL INTERPRETATION REQUIRED"
echo "This script gathers evidence; it does not evaluate the update policy. Read the"
echo "sections above — a failed step included — and record the conclusion and who"
echo "reached it. Until then, assume Caddy security updates need to be installed by hand."
