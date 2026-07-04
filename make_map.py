#!/usr/bin/env python
"""Backward-compatible shim → ``solargrader map``."""

import sys

from solargrader.cli import main

if __name__ == "__main__":
    main(["map", *sys.argv[1:]])
