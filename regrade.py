#!/usr/bin/env python
"""Backward-compatible shim → ``solargrader regrade``."""

import sys

from solargrader.cli import main

if __name__ == "__main__":
    main(["regrade", *sys.argv[1:]])
