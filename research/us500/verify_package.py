"""Verify delivered artifact hashes without importing the engine or using network."""

import hashlib
import json
from pathlib import Path


def verify(root):
    root = Path(root).resolve()
    manifest = json.loads((root / "MANIFIESTO_V2.json").read_text())
    actual = set()
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if path.is_symlink():
            raise ValueError("INVALID_MANIFEST_PATH")
        if any(
            part in {"__pycache__", ".ruff_cache", ".pytest_cache"}
            for part in relative.parts
        ):
            continue
        if path.is_file() and str(relative) != "MANIFIESTO_V2.json":
            actual.add(relative.as_posix())
    if actual != set(manifest["files"]):
        raise ValueError("MANIFEST_FILE_SET_MISMATCH")
    for relative, expected in manifest["files"].items():
        path = root / relative
        if path.is_symlink() or root not in path.resolve().parents:
            raise ValueError("INVALID_MANIFEST_PATH")
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError("FILE_HASH_MISMATCH: " + relative)
    return {"verified_files": len(manifest["files"]), "version": manifest["version"]}


def main():
    print(json.dumps(verify(Path(__file__).resolve().parent)))


if __name__ == "__main__":
    main()
