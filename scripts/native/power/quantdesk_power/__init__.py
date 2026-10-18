"""QuantDesk power controller: conditional, fail-safe shutdown of the one Azure VM.

Separate from the observer. It holds the only Azure credential on the
machine (the VM's managed identity, reachable only from its own cgroup), and
the observer holds none. See ../README.md.
"""
from pathlib import Path

VERSION = (Path(__file__).resolve().parent.parent / "VERSION").read_text().strip()
