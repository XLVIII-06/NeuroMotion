from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from neuromotion.paths import app_root, resource_path


class DesktopPathTests(unittest.TestCase):
    def test_source_paths_remain_in_gui_folder(self):
        with patch.object(sys, "frozen", False, create=True):
            self.assertEqual(app_root(), Path(__file__).resolve().parents[1])

    def test_frozen_writable_paths_follow_exe_not_bundle_or_working_directory(self):
        with TemporaryDirectory() as name:
            base = Path(name)
            with patch.object(sys, "frozen", True, create=True), patch.object(sys, "executable", str(base / "NeuroMotion.exe")):
                self.assertEqual(app_root(), base.resolve())
                self.assertNotEqual(app_root(), Path(sys.prefix))

    def test_bundled_defaults_allow_external_config_override(self):
        with TemporaryDirectory() as name:
            base = Path(name)
            external, bundled = base / "installed", base / "bundle"
            external.mkdir()
            (bundled / "neuromotion").mkdir(parents=True)
            (bundled / "default.json").write_text("bundled")
            with patch.object(sys, "frozen", True, create=True), patch.object(sys, "executable", str(external / "NeuroMotion.exe")), patch("neuromotion.paths.__file__", str(bundled / "neuromotion/paths.py")):
                self.assertEqual(resource_path("default.json"), bundled / "default.json")
                (external / "default.json").write_text("custom")
                self.assertEqual(resource_path("default.json"), external / "default.json")


if __name__ == "__main__":
    unittest.main()
