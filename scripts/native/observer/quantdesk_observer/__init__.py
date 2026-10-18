"""QuantDesk operational observer: an external, read-only reader of the desk.

It never writes to QuantDesk, never starts or restarts anything, and never
asks the desk to produce a signal. See ../README.md.
"""
from pathlib import Path

VERSION = (Path(__file__).resolve().parent.parent / "VERSION").read_text().strip()
