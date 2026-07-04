"""Compatibility shim so `pip install -e .` works on older pip (< 21.3), which
can't do editable installs from pyproject.toml alone. All real metadata lives in
pyproject.toml — this just delegates to setuptools."""

from setuptools import setup

setup()
