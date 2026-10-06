"""Offline cleanup regression tests; only creates/deletes isolated test fixtures.

Run from the project directory with Python 3.10+; set CLEAR_PROFILE_SCRIPT
to test another copy of the script.
"""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
SCRIPT = Path(os.environ.get('CLEAR_PROFILE_SCRIPT', '4.ClearProfile.py')).resolve()
sys.path.insert(0, str(SCRIPT.parent))
spec = importlib.util.spec_from_file_location('clear_profile_under_test', SCRIPT)
cleanup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cleanup)


class CleanupTests(unittest.TestCase):
    def setUp(self):
        root = Path(os.environ.get('CLEAR_PROFILE_TEST_ROOT', tempfile.gettempdir()))
        root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='cnki_cleanup_test_', dir=root)
        self.base = Path(self.temp.name)
        self.profile = self.base / 'browser-profile'
        self.profile.mkdir()
        self.links = []

    def tearDown(self):
        for link in reversed(self.links):
            if os.path.lexists(link):
                if os.name == 'nt' and not link.is_symlink():
                    os.rmdir(link)
                else:
                    link.unlink()
        self.temp.cleanup()

    def make_file(self, relative, data=b'fixture'):
        target = self.profile / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return target

    def invoke(self, *args):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            try:
                rc = cleanup.main(['--profile', str(self.profile), *args])
            except SystemExit as exc:
                rc = exc.code
        return rc, output.getvalue() + errors.getvalue()

    def make_directory_link(self, link, target):
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            if os.name != 'nt':
                self.skipTest('Directory links unavailable')
            result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(target)],
                                    capture_output=True, creationflags=0x08000000)
            if result.returncode:
                self.skipTest('Neither symlinks nor junctions available')
        self.links.append(link)

    @unittest.skipUnless(os.name == 'nt', 'Windows paths are case insensitive')
    def test_home_case_variant_is_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cleanup.assert_safe(os.path.expanduser('~').swapcase())

    @unittest.skipUnless(os.name == 'nt', 'Windows paths are case insensitive')
    def test_tool_case_variant_is_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cleanup.assert_safe(cleanup.SCRIPT_DIR.swapcase())

    def test_personal_folder_case_variant_is_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cleanup.assert_safe(str(self.base / 'dEsKtOp'))

    def test_tool_ancestor_is_rejected(self):
        with patch.object(cleanup, 'SCRIPT_DIR', str(self.profile / 'tool')):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                cleanup.assert_safe(str(self.profile))

    def test_filesystem_root_is_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cleanup.assert_safe(self.base.anchor)

    def test_normal_cache_removed_and_credentials_preserved(self):
        cache = self.make_file('Default/Cache/item')
        cookies = self.make_file('Default/Network/Cookies', b'private fixture')
        login = self.make_file('Default/Login Data', b'login fixture')
        prefs = self.make_file('Default/Preferences', b'preferences fixture')
        state = self.make_file('Local State', b'state fixture')
        rc, _ = self.invoke()
        self.assertEqual(rc, 0)
        self.assertFalse(cache.exists())
        for file, value in [(cookies,b'private fixture'), (login,b'login fixture'),
                            (prefs,b'preferences fixture'), (state,b'state fixture')]:
            self.assertEqual(file.read_bytes(), value)

    def test_whitelisted_cache_file_removed(self):
        cache_file = self.make_file('Default/Network/TransportSecurity')
        rc, _ = self.invoke()
        self.assertEqual(rc, 0)
        self.assertFalse(cache_file.exists())

    def test_dry_run_preserves_cache(self):
        cache = self.make_file('Default/Cache/item')
        rc, _ = self.invoke('--dry-run')
        self.assertEqual(rc, 0)
        self.assertEqual(cache.read_bytes(), b'fixture')

    def test_full_delete_requires_confirmation(self):
        cookies = self.make_file('Default/Network/Cookies')
        rc, _ = self.invoke('--all')
        self.assertNotEqual(rc, 0)
        self.assertTrue(cookies.exists())

    def test_full_delete_dry_run_preserves_credentials(self):
        cookies = self.make_file('Default/Network/Cookies')
        rc, _ = self.invoke('--all', '--confirm-custom-profile', '--dry-run')
        self.assertEqual(rc, 0)
        self.assertTrue(cookies.exists())

    def test_full_delete_removes_profile(self):
        self.make_file('Default/Network/Cookies')
        rc, _ = self.invoke('--all', '--confirm-custom-profile')
        self.assertEqual(rc, 0)
        self.assertFalse(self.profile.exists())

    def test_full_delete_failure_returns_error(self):
        cookies = self.make_file('Default/Network/Cookies')
        real_remove = cleanup.shutil.rmtree
        def deny(path, *args, **kwargs):
            if Path(path) == self.profile:
                if kwargs.get('ignore_errors'):
                    return
                raise PermissionError('fixture: browser files locked')
            return real_remove(path, *args, **kwargs)
        with patch.object(cleanup.shutil, 'rmtree', side_effect=deny):
            rc, output = self.invoke('--all', '--confirm-custom-profile')
        self.assertNotEqual(rc, 0)
        self.assertTrue(cookies.exists())
        self.assertNotIn('已删除整个 profile', output)

    def test_full_delete_leftover_returns_error(self):
        self.make_file('Default/Network/Cookies')
        with patch.object(cleanup.shutil, 'rmtree', return_value=None):
            rc, output = self.invoke('--all', '--confirm-custom-profile')
        self.assertNotEqual(rc, 0)
        self.assertTrue(self.profile.exists())
        self.assertNotIn('已删除整个 profile', output)

    def test_cache_failure_is_reported_and_other_cache_can_be_removed(self):
        locked = self.make_file('Default/Cache/item')
        other = self.make_file('Default/GPUCache/item')
        real_remove = cleanup.shutil.rmtree
        def deny(path, *args, **kwargs):
            if Path(path) == locked.parent:
                if kwargs.get('ignore_errors'):
                    return
                raise PermissionError('fixture: cache locked')
            return real_remove(path, *args, **kwargs)
        with patch.object(cleanup.shutil, 'rmtree', side_effect=deny):
            rc, output = self.invoke()
        self.assertNotEqual(rc, 0)
        self.assertTrue(locked.exists())
        self.assertFalse(other.exists())
        self.assertNotIn('已删  Default/Cache', output)

    def test_linked_profile_is_rejected(self):
        target = self.base / 'outside'
        target.mkdir()
        link = self.base / 'profile-link'
        self.make_directory_link(link, target)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cleanup.assert_safe(str(link))

    def test_linked_cache_parent_cannot_delete_external_cache(self):
        target = self.base / 'outside'
        (target / 'Cache').mkdir(parents=True)
        sentinel = target / 'Cache' / 'keep'
        sentinel.write_bytes(b'external cache fixture')
        self.make_directory_link(self.profile / 'Default', target)
        rc, _ = self.invoke()
        self.assertNotEqual(rc, 0)
        self.assertTrue(sentinel.exists())
        self.assertEqual(sentinel.read_bytes(), b'external cache fixture')


if __name__ == '__main__':
    unittest.main(verbosity=2)
