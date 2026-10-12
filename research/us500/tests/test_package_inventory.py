import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from verify_package import verify


class InventoryTests(unittest.TestCase):
    def test_isolated_entrypoint_rejects_shadow_without_importing_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = Path(__file__).resolve().parents[1] / "verify_package.py"
            (root / source.name).write_bytes(source.read_bytes())
            (root / "MANIFIESTO_V2.json").write_text(
                json.dumps(
                    {
                        "version": "test",
                        "files": {
                            source.name: hashlib.sha256(source.read_bytes()).hexdigest()
                        },
                    }
                )
            )
            (root / "pathlib.py").write_text(
                "open('SHADOW_EXECUTED', 'w').write('bad')\nraise RuntimeError('SHADOW')\n"
            )
            result = subprocess.run(
                [sys.executable, "-I", str(root / source.name)],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("MANIFEST_FILE_SET_MISMATCH", result.stderr)
            self.assertFalse((root / "SHADOW_EXECUTED").exists())

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
