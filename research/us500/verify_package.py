"""Verify delivered artifact hashes without importing the engine or using network."""

import hashlib
import json
from pathlib import Path


def main():
    root = Path(__file__).resolve().parent
    manifest = json.loads((root / "MANIFIESTO_V2.json").read_text())
    for relative, expected in manifest["files"].items():
        path = root / relative
        if path.is_symlink() or root not in path.resolve().parents:
            raise ValueError("INVALID_MANIFEST_PATH")
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError("FILE_HASH_MISMATCH: " + relative)
    print(
        json.dumps(
            {"verified_files": len(manifest["files"]), "version": manifest["version"]}
        )
    )


if __name__ == "__main__":
    main()
