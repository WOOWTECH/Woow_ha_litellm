"""Installer unit tests; set LITELLM_LOGIN_BASELINE to pristine pinned source.

Run separately from the installed-image contracts; no full vendor-source fixture.
LITELLM_LOGIN_INSTALLER optionally selects the identical installer in another repo.
"""
import hashlib
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

INSTALLER = Path(os.environ.get('LITELLM_LOGIN_INSTALLER',
    str(Path(__file__).resolve().parents[2] / 'litellm/patches/apply_login_throttle.py')))
spec = importlib.util.spec_from_file_location('login_security_installer', INSTALLER)
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class InstallerContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = os.environ.get('LITELLM_LOGIN_BASELINE')
        if not path:
            raise RuntimeError('LITELLM_LOGIN_BASELINE must name pristine pinned login_throttle.py')
        cls.source = Path(path).read_bytes()
        if hashlib.sha256(cls.source).hexdigest() != installer.EXPECTED:
            raise RuntimeError('Baseline source hash differs')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'litellm'
        self.target = self.root / installer.TARGET
        self.target.parent.mkdir(parents=True)
        self.target.write_bytes(self.source)
        self.other = self.root / 'untouched.py'
        self.other.write_text('# untouched\n')

    def test_exact_source_produces_exact_output_and_only_target_changes(self):
        self.target.chmod(0o640)
        installer.apply(self.root)
        self.assertEqual(hashlib.sha256(self.target.read_bytes()).hexdigest(), installer.RESULT)
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o640)
        self.assertEqual(self.other.read_text(), '# untouched\n')
        compile(self.target.read_bytes(), installer.TARGET, 'exec')

    def test_changed_source_refuses_without_write(self):
        changed = self.source + b'\n# drift\n'
        self.target.write_bytes(changed)
        with self.assertRaises(SystemExit):
            installer.apply(self.root)
        self.assertEqual(self.target.read_bytes(), changed)

    def test_already_patched_source_refuses_without_write(self):
        installer.apply(self.root)
        before = self.target.read_bytes()
        with self.assertRaises(SystemExit):
            installer.apply(self.root)
        self.assertEqual(self.target.read_bytes(), before)

    def test_modified_payload_result_guard_refuses_without_write(self):
        with patch.object(installer, 'SAFE_BLOCKED', installer.SAFE_BLOCKED + '\n# changed'), self.assertRaises(SystemExit):
            installer.apply(self.root)
        self.assertEqual(self.target.read_bytes(), self.source)

    def test_missing_target_refuses(self):
        self.target.unlink()
        with self.assertRaises(SystemExit):
            installer.apply(self.root)
        self.assertFalse(self.target.exists())

    def test_symlink_target_refuses_without_touching_destination(self):
        outside = Path(self.tmp.name) / 'outside.py'
        outside.write_bytes(self.source)
        self.target.unlink()
        self.target.symlink_to(outside)
        with self.assertRaises(SystemExit):
            installer.apply(self.root)
        self.assertEqual(outside.read_bytes(), self.source)

    def test_symlink_parent_and_package_root_refuse(self):
        alias = Path(self.tmp.name) / 'alias'
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(SystemExit):
            installer.apply(alias)
        original = self.root / 'original-auth'
        self.target.parent.rename(original)
        (self.root / 'proxy/auth').symlink_to(original, target_is_directory=True)
        with self.assertRaises(SystemExit):
            installer.apply(self.root)
        self.assertEqual((original / 'login_throttle.py').read_bytes(), self.source)


if __name__ == '__main__':
    unittest.main(verbosity=2)
