"""Shared JSON I/O utilities."""

import json
import os


def load_json(path):
    """Read and parse a JSON file. Returns None on any error."""
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
