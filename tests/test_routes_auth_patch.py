"""Portable build-guard regressions; runtime authentication lives in api_compat."""
import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('routes_installer', ROOT / 'litellm/patches/apply_routes_auth.py')
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class InstallerContract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.target = self.root / installer.TARGET
        self.target.parent.mkdir(parents=True)
        # Exercise fail-closed behavior without vendoring the entire upstream types
        # module. Docker installation and ASGI contracts verify the real pinned input.
        before = ('class Routes:\n' + installer.BEFORE + '            "/health/liveness",\n        )\n    )\n').encode()
        after = before.replace(installer.BEFORE.encode(), installer.AFTER.encode(), 1)
        self.target.write_bytes(before)
        for name, value in [('EXPECTED', hashlib.sha256(before).hexdigest()),
                            ('RESULT', hashlib.sha256(after).hexdigest())]:
            guard = patch.object(installer, name, value)
            guard.start()
            self.addCleanup(guard.stop)

    def test_exact_patch_and_repeat_refusal(self):
        installer.apply(self.root)
        self.assertEqual(hashlib.sha256(self.target.read_bytes()).hexdigest(), installer.RESULT)
        before = self.target.read_bytes()
        with self.assertRaises(SystemExit):
            installer.apply(self.root)
        self.assertEqual(self.target.read_bytes(), before)

    def test_input_drift_refused_without_write(self):
        self.target.write_bytes(self.target.read_bytes() + b'\n# drift\n')
        before = self.target.read_bytes()
        with self.assertRaises(SystemExit):
            installer.apply(self.root)
        self.assertEqual(self.target.read_bytes(), before)

    def test_symlink_refused_without_destination_write(self):
        other = self.root / 'other.py'
        self.target.rename(other)
        self.target.symlink_to(other)
        before = other.read_bytes()
        with self.assertRaises(SystemExit):
            installer.apply(self.root)
        self.assertEqual(other.read_bytes(), before)

    def test_output_drift_refused_without_write(self):
        before = self.target.read_bytes()
        with patch.object(installer, 'RESULT', '0' * 64), self.assertRaises(SystemExit):
            installer.apply(self.root)
        self.assertEqual(self.target.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
