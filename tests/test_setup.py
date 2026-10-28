import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
import controller


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.proxy = self.root/'mitmweb'; self.proxy.touch()
        (self.root/'mitmdump').touch()
        self.ca = self.root/'ca/mitmproxy-ca-cert.pem'
        self.settings = {'mitmweb':str(self.proxy), 'ca_file':str(self.ca)}

    def tearDown(self):
        self.temp.cleanup()

    @mock.patch.object(controller, 'rpc', return_value={'running':False})
    def test_incomplete_ca_is_never_replaced(self, _):
        self.ca.parent.mkdir(); self.ca.write_text('existing public certificate')
        with mock.patch.object(controller.subprocess, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'incomplete CA'):
                controller.prepare_setup(self.settings)
            run.assert_not_called()
        self.assertEqual(self.ca.read_text(), 'existing public certificate')

    @mock.patch.object(controller, 'rpc', return_value={'running':False})
    @mock.patch.object(controller, 'ca_is_trusted', return_value=False)
    def test_existing_ca_is_reused_without_generation_or_trust(self, *_):
        self.ca.parent.mkdir(); self.ca.write_text('public')
        key = self.ca.parent/'mitmproxy-ca.pem'; key.write_text('private')
        with mock.patch.object(controller.subprocess, 'run') as run:
            result = controller.prepare_setup(self.settings)
            run.assert_not_called()
        self.assertFalse(result['certificate_trusted'])
        self.assertEqual(key.read_text(), 'private')

    @mock.patch.object(controller, 'rpc', return_value={'running':False})
    @mock.patch.object(controller, 'ca_is_trusted', return_value=False)
    def test_generation_has_no_listener_and_restricts_private_key(self, *_):
        def generate(command, **kwargs):
            self.assertIn('server=false', command)
            self.assertNotIn('security', command)
            self.ca.write_text('synthetic public fixture')
            key=self.ca.parent/'mitmproxy-ca.pem'; key.write_text('synthetic private fixture'); key.chmod(0o644)
            return mock.Mock(returncode=0, stdout=b'', stderr=b'')
        with mock.patch.object(controller.subprocess, 'run', side_effect=generate):
            controller.prepare_setup(self.settings)
        self.assertEqual((self.ca.parent/'mitmproxy-ca.pem').stat().st_mode & 0o777, 0o600)

    def test_frozen_worker_uses_its_executable_without_a_python_script(self):
        with mock.patch.object(controller.sys, 'frozen', True, create=True):
            command=controller.worker_command((18741,18742))
        self.assertEqual(command, [sys.executable,'worker','--ports','18741','18742'])

    def test_long_data_paths_use_a_private_short_socket_path(self):
        with mock.patch.object(controller, 'DATA', self.root/('long-folder-'*12)):
            path = controller.control_socket()
            self.assertLess(len(os.fsencode(path)), 104)
            self.assertEqual(path.parent.stat().st_uid, os.getuid())
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(path, controller.control_socket())
        with mock.patch.object(controller, 'DATA', Path('/tmp/short-profile')):
            self.assertEqual(controller.control_socket(), Path('/tmp/short-profile/control.sock'))

    @mock.patch.object(controller, 'rpc', return_value={'running':False})
    def test_data_move_updates_an_internal_certificate_path(self, _):
        profile=self.root/'profile'; profile.mkdir()
        certificate=profile/'CertificateAuthority/mitmproxy-ca-cert.pem'
        certificate.parent.mkdir(); certificate.write_text('synthetic certificate')
        (certificate.parent/'mitmproxy-ca.pem').write_text('synthetic private key')
        target=self.root/'moved-profile'
        settings={'gpt_app':str(self.proxy),'mitmweb':str(self.proxy),'ca_file':str(certificate),
                  'proxy_port':18831,'web_port':18832}
        with mock.patch.object(controller, 'DATA', profile), mock.patch.object(controller, 'LOCATION_FILE', self.root/'location.json'):
            controller.save_settings(dict(settings,data_dir=str(target)),settings)
        saved=json.loads((target/'settings.json').read_text())
        self.assertEqual(Path(saved['ca_file']),target/'CertificateAuthority/mitmproxy-ca-cert.pem')
        self.assertTrue(certificate.exists())
        self.assertEqual((target/'CertificateAuthority/mitmproxy-ca.pem').read_text(),'synthetic private key')

    @mock.patch.object(controller, 'rpc', return_value={'running':True})
    def test_setup_does_not_change_a_running_proxy(self, _):
        with self.assertRaisesRegex(ValueError, 'Stop the managed proxy'):
            controller.prepare_setup(self.settings)


if __name__ == '__main__':
    unittest.main()
