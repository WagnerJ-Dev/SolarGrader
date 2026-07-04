#!/usr/bin/env python
"""Backward-compatible shim → ``solargrader enrich``."""

import sys

from solargrader.cli import main

if __name__ == "__main__":
    main(["enrich", *sys.argv[1:]])
