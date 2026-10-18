#!/usr/bin/env bash
# Remove what the staging trial stored — its ACME account, its certificate —
# once the trial is over. One directory, written here in full and taken from
# nowhere else: no setting, argument or environment variable can point this
# script at another. It is the directory the Caddyfile gives to staging alone
# (certificates-staging). Nothing is searched for and nothing is matched by
# pattern; production's storage is a different directory beside it and is not
# looked at.
#
# Run on the VM with Caddy stopped. It removes nothing unless it has
# established, positively, that Caddy is stopped and that the path is a real
# directory reached through real directories. Anything it cannot establish —
# a state it does not recognise, a check that fails, a link anywhere along the
# path — stops it. It does not start, stop or restart Caddy.
#
# The two checks on Caddy are looks at one moment. They are not a lock:
# nothing here keeps Caddy from being started a second later. Caddy has to
# stay stopped for the whole of the cleanup, and that is the operator's to
# ensure — which is why this is run only inside the after-market deployment
# procedure (README.md, "Hostname and certificate"), not on its own.
set -uo pipefail
readonly TRIAL=/var/lib/caddy/quantdesk/staging-trial

stop() {
  echo "STOPPED: $*"
  echo "Nothing was removed."
  exit 1
}

# Caddy is stopped only if systemd says so in as many words. `is-active`
# exits non-zero for every state that is not "active" — and also when
# systemctl is missing, cannot reach systemd, or is refused — so the exit
# status alone says nothing; the state it names has to be one that means
# stopped.
STATE="$(systemctl is-active caddy 2>&1)"
STATUS=$?
case "$STATUS:$STATE" in
  3:inactive | 3:failed) ;;
  0:*) stop "Caddy is running (systemctl is-active caddy: ${STATE:-no output}). Stop it first." ;;
  *) stop "could not establish that Caddy is stopped" \
          "(systemctl is-active caddy exited $STATUS: ${STATE:-no output})." ;;
esac
# And no caddy process outside systemd's view. Of pgrep's exit statuses only
# one says "I looked and there is none": 1. 0 is a caddy. Everything else —
# 2 and 3 for its own errors, 126 and 127 when it cannot be run or is not
# installed, anything unexpected — is a look that did not happen, and is not
# read as "no Caddy".
pgrep -x caddy >/dev/null 2>&1
FOUND=$?
case "$FOUND" in
  1) ;;
  0) stop "a caddy process is running." ;;
  *) stop "could not check for a running caddy process (pgrep -x caddy exited $FOUND)." ;;
esac

# Every directory on the way down is a real directory, not a link: a link at
# any level would have the removal happen somewhere else.
sudo true || stop "sudo is not available."
for part in /var /var/lib /var/lib/caddy /var/lib/caddy/quantdesk; do
  if sudo test -L "$part"; then stop "$part is a link, not a directory."; fi
  if ! sudo test -e "$part"; then
    echo "nothing to clear: $part does not exist"
    exit 0
  fi
  sudo test -d "$part" || stop "$part is not a directory."
done
if sudo test -L "$TRIAL"; then stop "$TRIAL is a link, not a directory."; fi
if ! sudo test -e "$TRIAL"; then
  echo "nothing to clear: $TRIAL does not exist"
  exit 0
fi
sudo test -d "$TRIAL" || stop "$TRIAL is not a directory."

sudo rm -rf -- "$TRIAL" || stop "removing $TRIAL failed part-way; look at what is left of it."
echo "removed $TRIAL"
echo "not touched: /var/lib/caddy/quantdesk/production"
