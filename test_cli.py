"""Local setup tests with no real credentials or requests."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import bili_cli
from binding import ProcessBinding


class SetupTests(unittest.TestCase):
    def test_init_disabled_and_preserves_existing_policies(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(bili_cli, "PROCESS_BINDING", ProcessBinding("fixture", "123")):
            root = Path(temporary)
            result = bili_cli.initialize(root)
            self.assertFalse(result["all_writes_enabled"])
            social = json.loads((root / "bili_social_policy.json").read_text())
            dm = json.loads((root / "bili_dm_policy.json").read_text())
            self.assertFalse(any(social["permissions"].values()))
            self.assertFalse(dm["send_enabled"])
            before = (root / "bili_social_policy.json").read_bytes()
            with self.assertRaises(bili_cli.SetupError):
                bili_cli.initialize(root)
            self.assertEqual(before, (root / "bili_social_policy.json").read_bytes())

    def test_owner_required_before_state_creation(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(bili_cli, "PROCESS_BINDING", ProcessBinding()):
            root = Path(temporary) / "new"
            with self.assertRaises(bili_cli.SetupError):
                bili_cli.initialize(root)
            self.assertFalse(root.exists())

    def test_atomic_private_file_keeps_old_file_on_serialization_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "fixture.json"
            bili_cli.private_json(path, {"fixture": True})
            before = path.read_bytes()
            with self.assertRaises(TypeError):
                bili_cli.private_json(path, {"fixture": object()})
            self.assertEqual(before, path.read_bytes())
            self.assertEqual([p.name for p in Path(temporary).iterdir()], ["fixture.json"])


if __name__ == "__main__":
    unittest.main()
