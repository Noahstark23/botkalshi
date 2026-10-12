import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from verify_package import verify


class InventoryTests(unittest.TestCase):
    def test_exact_inventory_hash_and_symlink_protection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact = root / "code.py"
            artifact.write_text("pass\n")
            manifest = {
                "version": "test",
                "files": {"code.py": hashlib.sha256(artifact.read_bytes()).hexdigest()},
            }
            (root / "MANIFIESTO_V2.json").write_text(json.dumps(manifest))
            self.assertEqual(verify(root)["verified_files"], 1)
            extra = root / "unreviewed.py"
            extra.write_text("pass\n")
            with self.assertRaisesRegex(ValueError, "FILE_SET"):
                verify(root)
            extra.unlink()
            artifact.write_text("changed\n")
            with self.assertRaisesRegex(ValueError, "HASH_MISMATCH"):
                verify(root)
            artifact.unlink()
            artifact.symlink_to("/dev/null")
            with self.assertRaisesRegex(ValueError, "INVALID_MANIFEST_PATH"):
                verify(root)


if __name__ == "__main__":
    unittest.main()
