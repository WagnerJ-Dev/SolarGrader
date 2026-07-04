#!/usr/bin/env python
"""Backward-compatible shim → ``solargrader run``.

The pipeline now lives in the ``solargrader`` package. This keeps the old command
working:  python pipeline.py --region harrisburg   ==   solargrader run --region harrisburg
"""

import sys

from solargrader.cli import main

if __name__ == "__main__":
    main(["run", *sys.argv[1:]])
