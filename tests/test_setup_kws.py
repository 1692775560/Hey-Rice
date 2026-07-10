import tempfile
import subprocess
import sys
import unittest
from pathlib import Path

from scripts.setup_kws import KEYWORD_LINE, model_is_ready, write_keywords
from wakeword import REQUIRED_FILES


ROOT = Path(__file__).resolve().parents[1]


class SetupKwsTests(unittest.TestCase):
    def test_script_can_be_executed_directly(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "setup_kws.py"), "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Download and prepare", result.stdout)

    def test_keyword_file_contains_xiaogua_tokens(self):
        with tempfile.TemporaryDirectory() as root:
            model_dir = Path(root)

            write_keywords(model_dir)

            self.assertEqual(
                (model_dir / "keywords.txt").read_text(encoding="utf-8"),
                KEYWORD_LINE + "\n",
            )
            self.assertIn("@小瓜小瓜", KEYWORD_LINE)

    def test_model_ready_requires_every_runtime_file(self):
        with tempfile.TemporaryDirectory() as root:
            model_dir = Path(root)
            for filename in REQUIRED_FILES:
                (model_dir / filename).write_bytes(b"ok")

            self.assertTrue(model_is_ready(model_dir))
            (model_dir / REQUIRED_FILES[0]).unlink()
            self.assertFalse(model_is_ready(model_dir))


if __name__ == "__main__":
    unittest.main()
