from __future__ import annotations

from pathlib import Path
import shutil
import unittest
import uuid
from unittest.mock import patch

from codexsync.cli import _resolve_config_path, main


class CliInitConfigTests(unittest.TestCase):
    def test_init_config_writes_template_file(self) -> None:
        root = Path.cwd() / "test-sandbox" / f"init-config-{uuid.uuid4().hex}"
        output = root / "config.toml"
        try:
            code = main(["init-config", "--output", str(output)])
            self.assertEqual(code, 0)
            self.assertTrue(output.exists())
            text = output.read_text(encoding="utf-8")
            self.assertIn("[sync]", text)
            self.assertIn("[paths]", text)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_init_config_requires_force_for_overwrite(self) -> None:
        root = Path.cwd() / "test-sandbox" / f"init-config-force-{uuid.uuid4().hex}"
        output = root / "config.toml"
        try:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text("original", encoding="utf-8")
            code = main(["init-config", "--output", str(output)])
            self.assertEqual(code, 4)
            self.assertEqual(output.read_text(encoding="utf-8"), "original")
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_init_config_force_overwrites_existing_file(self) -> None:
        root = Path.cwd() / "test-sandbox" / f"init-config-overwrite-{uuid.uuid4().hex}"
        output = root / "config.toml"
        try:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text("old", encoding="utf-8")
            code = main(["init-config", "--output", str(output), "--force"])
            self.assertEqual(code, 0)
            text = output.read_text(encoding="utf-8")
            self.assertIn("[sync]", text)
            self.assertNotEqual(text, "old")
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_config_resolution_prefers_current_directory_config(self) -> None:
        root = Path.cwd() / "test-sandbox" / f"config-resolution-{uuid.uuid4().hex}"
        root.mkdir(parents=True, exist_ok=True)
        previous = Path.cwd()
        try:
            expected = root / "config.toml"
            expected.write_text("", encoding="utf-8")
            import os
            os.chdir(root)
            self.assertEqual(_resolve_config_path(None), expected)
        finally:
            import os
            os.chdir(previous)
            shutil.rmtree(root, ignore_errors=True)

    def test_config_resolution_falls_back_to_user_config(self) -> None:
        root = Path.cwd() / "test-sandbox" / f"config-no-local-{uuid.uuid4().hex}"
        home = Path.cwd() / "test-sandbox" / f"config-home-{uuid.uuid4().hex}"
        root.mkdir(parents=True, exist_ok=True)
        previous = Path.cwd()
        try:
            import os
            os.chdir(root)
            with patch("codexsync.cli.Path.home", return_value=home):
                self.assertEqual(_resolve_config_path(None), home / ".codexsync" / "config.toml")
        finally:
            import os
            os.chdir(previous)
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
