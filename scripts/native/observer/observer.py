#!/usr/bin/env python3
"""Entry point for the QuantDesk operational observer.

Run as `python3 -I observer.py <check> --config <file>`. `-I` keeps the
environment and the working directory off the import path, so the only code
this loads is the package beside this file and the standard library.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from quantdesk_observer.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
