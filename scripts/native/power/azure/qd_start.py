"""Azure Automation runbook `qd-start` (Python 3): start the QuantDesk VM.

Scheduled at 07:30 IST, Monday to Friday, holidays included (a reviewed
holiday still starts the VM: the observer reports NO SESSION and the hosted
checks see their normal heartbeats). A special session gets a one-off schedule
of this same runbook (power.py plan-special).

It reads the power state and starts the VM only if it is deallocated or
stopped; if it is already running or starting it does nothing. It then waits
up to ten minutes for `running`, reading the power state (positive evidence)
rather than inferring success. Anything else fails the job, which alerts
(`qd-runbook-failed`); the 07:55 monitor run and the hosted heartbeat (due
07:45, alert by 07:55) catch a VM that is not up.

Identity: the Automation account's managed identity, with only
virtualMachines/read, virtualMachines/instanceView/read and
virtualMachines/start/action on this one VM (role-automation.json).

Self-contained: an Automation runbook cannot import another runbook.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

ARM = "https://management.azure.com"
API_VERSION = "2024-07-01"
VM_ID = re.compile(r"^/subscriptions/[0-9a-f-]{36}/resourceGroups/[A-Za-z0-9._()-]{1,90}/"
                   r"providers/Microsoft\.Compute/virtualMachines/[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
START_WAIT_SECONDS, POLL_SECONDS = 600, 15


def _variable(name: str) -> str | None:
    try:
        import automationassets  # type: ignore  # provided by Azure Automation
        return automationassets.get_automation_variable(name)
    except ImportError:
        return os.environ.get(name)


def _token(opener=urllib.request.urlopen) -> str:
    endpoint, secret = os.environ.get("IDENTITY_ENDPOINT"), os.environ.get("IDENTITY_HEADER")
    if not endpoint or not secret:
        raise RuntimeError("no managed identity is available to this runbook")
    query = urllib.parse.urlencode({"resource": "https://management.azure.com/",
                                    "api-version": "2019-08-01"})
    request = urllib.request.Request(f"{endpoint}?{query}", headers={
        "X-IDENTITY-HEADER": secret, "Metadata": "true"})
    with opener(request, timeout=10) as response:
        return json.loads(response.read(65536))["access_token"]


def power_state(vm_id: str, token: str, opener=urllib.request.urlopen) -> str:
    request = urllib.request.Request(f"{ARM}{vm_id}/instanceView?api-version={API_VERSION}",
                                     headers={"Authorization": f"Bearer {token}"})
    with opener(request, timeout=20) as response:
        view = json.loads(response.read(1024 * 1024))
    codes = [s.get("code", "") for s in view.get("statuses", []) if isinstance(s, dict)]
    states = [c.split("/", 1)[1] for c in codes if c.startswith("PowerState/")]
    return states[0] if len(states) == 1 else "unknown"


def start(vm_id: str, token: str, opener=urllib.request.urlopen, sleep=time.sleep,
          monotonic=time.monotonic) -> str:
    state = power_state(vm_id, token, opener)
    if state in ("running", "starting"):
        return f"already {state}; nothing done"
    if state not in ("deallocated", "stopped"):
        raise RuntimeError(f"the VM is {state}; not starting it")
    request = urllib.request.Request(f"{ARM}{vm_id}/start?api-version={API_VERSION}", data=b"",
                                     method="POST",
                                     headers={"Authorization": f"Bearer {token}",
                                              "Content-Length": "0"})
    with opener(request, timeout=20) as response:
        if response.status not in (200, 202):
            raise RuntimeError(f"Azure answered {response.status} to the start request")
    deadline = monotonic() + START_WAIT_SECONDS
    while monotonic() < deadline:
        sleep(POLL_SECONDS)
        state = power_state(vm_id, token, opener)
        if state == "running":
            return "started"
    raise RuntimeError(f"the VM is {state} ten minutes after the start request")


def main(opener=urllib.request.urlopen, sleep=time.sleep) -> int:
    vm_id = _variable("QD_VM_RESOURCE_ID")
    if not vm_id or not VM_ID.match(vm_id):
        raise RuntimeError("QD_VM_RESOURCE_ID is missing or malformed")
    print(f"qd-start: {start(vm_id, _token(opener), opener, sleep)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
