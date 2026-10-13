import hashlib
import importlib.util
import os
from pathlib import Path
import shutil
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[2]
SPEC=importlib.util.spec_from_file_location('installer',ROOT/'litellm/patches/apply_security_headers.py')
installer=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(installer)
BASE=Path(os.environ.get('LITELLM_BASELINE_MODULE', Path(__file__).with_name('fixtures')/'upstream_security_headers_middleware.py'))


class InstallerContract(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.target=self.root/installer.TARGET
        self.target.parent.mkdir(parents=True);shutil.copyfile(BASE,self.target)

    def test_exact_input_installs_exact_compilable_output(self):
        installer.apply(self.root)
        self.assertEqual(hashlib.sha256(self.target.read_bytes()).hexdigest(),installer.RESULT)
        compile(self.target.read_bytes(),str(self.target),'exec')

    def test_changed_source_refused_without_writing(self):
        self.target.write_bytes(self.target.read_bytes()+b'\n# upstream drift\n');before=self.target.read_bytes()
        with self.assertRaises(SystemExit):installer.apply(self.root)
        self.assertEqual(self.target.read_bytes(),before)

    def test_second_apply_refused_without_writing(self):
        installer.apply(self.root);before=self.target.read_bytes()
        with self.assertRaises(SystemExit):installer.apply(self.root)
        self.assertEqual(self.target.read_bytes(),before)

    def test_symlink_target_refused_without_writing_destination(self):
        original=self.root/'other.py';self.target.rename(original);self.target.symlink_to(original)
        with self.assertRaises(SystemExit):installer.apply(self.root)
        self.assertEqual(hashlib.sha256(original.read_bytes()).hexdigest(),installer.EXPECTED)

    def test_modified_payload_refused(self):
        patchdir=self.root/'installer';patchdir.mkdir();shutil.copyfile(ROOT/'litellm/patches/apply_security_headers.py',patchdir/'apply.py')
        (patchdir/'security_headers').mkdir();(patchdir/'security_headers/security_headers_middleware.py').write_text('raise RuntimeError("modified")')
        spec=importlib.util.spec_from_file_location('tampered_installer',patchdir/'apply.py');module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with self.assertRaises(SystemExit):module.apply(self.root)
        self.assertEqual(hashlib.sha256(self.target.read_bytes()).hexdigest(),installer.EXPECTED)


if __name__=='__main__':unittest.main(verbosity=2)
