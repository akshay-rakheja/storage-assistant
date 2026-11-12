#!/usr/bin/env python3
"""Project-local launcher; no installation or global configuration needed."""
import sys

sys.dont_write_bytecode = True  # leave no __pycache__ in the installed plugin folder

from storage_assistant.__main__ import main  # noqa: E402

if __name__ == "__main__":
    main()
