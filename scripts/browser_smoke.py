"""Compatibility entry point for the current catalog UI checks."""
import runpy
from pathlib import Path

if __name__ == "__main__":
    runpy.run_path(str(Path(__file__).with_name("browser_catalog_smoke.py")), run_name="__main__")
