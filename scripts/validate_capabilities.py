"""Validate file-based capability definitions, optionally export a canonical bundle."""
import argparse
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.capability_catalog import CapabilityCatalog
from app.store import Store


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, default=ROOT / "capabilities")
    parser.add_argument("--export", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as temporary:
        catalog = CapabilityCatalog(Store(Path(temporary) / "validation.db"), args.directory)
        snapshot = catalog.snapshot()
        if args.export:
            args.export.parent.mkdir(parents=True, exist_ok=True)
            args.export.write_text(json.dumps(snapshot.bundle, ensure_ascii=False, indent=2)+"\n")
        print(json.dumps({"valid": True, "catalog_revision": snapshot.revision, "counts": {k: len(snapshot.bundle[k]) for k in ["directories", "tools", "profiles", "scenarios"]}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
