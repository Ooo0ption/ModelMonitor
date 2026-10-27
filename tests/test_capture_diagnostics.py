from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from capture_diagnostics import current_issues, record_failure, record_success, tls_host
from controller import verify_ca, CertificateTrustError


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.health = {'session': 'test', 'tls_hosts': {}}

    def fail(self, times=(100, 105, 110), host='example.test'):
        for t in times:
            record_failure(self.health, host, 'peer rejected certificate', t)

    def test_old_warning_stopped_proxy_and_single_failure_stay_quiet(self):
        self.assertEqual(current_issues({'tls_warning': 'old warning'}, True, 110), [])
        self.fail([100])
        self.assertEqual(current_issues(self.health, True, 110), [])
        self.fail([105, 110])
        self.assertEqual(current_issues(self.health, False, 110), [])
        self.assertEqual(current_issues(self.health, True, 171), [])

    def test_sustained_incident_stable_id_and_recovery_per_host(self):
        self.fail()
        issue = current_issues(self.health, True, 110)[0]
        self.assertIn('example.test', issue['message'])
        self.assertIn('peer rejected certificate', issue['message'])
        self.fail([120, 130])
        self.assertEqual(current_issues(self.health, True, 130)[0]['id'], issue['id'])
        record_success(self.health, 'other.test', 131)
        self.assertEqual(len(current_issues(self.health, True, 131)), 1)
        record_success(self.health, 'example.test', 132)
        self.assertEqual(current_issues(self.health, True, 133), [])
        self.fail([140, 145, 150])
        self.assertNotEqual(current_issues(self.health, True, 150)[0]['id'], issue['id'])

    def test_burst_waits_for_persistence_and_multiple_hosts_are_reported(self):
        self.fail([100, 100.1, 100.2])
        self.assertEqual(current_issues(self.health, True, 101), [])
        self.assertEqual(current_issues(self.health, True, 110), [])
        self.fail([110])
        self.fail(host='other.test')
        self.assertEqual(len(current_issues(self.health, True, 110)), 2)

    def test_destination_uses_sni_then_server_address(self):
        context = SimpleNamespace(server=SimpleNamespace(address=('server.test', 443), sni=None))
        self.assertEqual(tls_host(SimpleNamespace(conn=SimpleNamespace(sni='Example.TEST'), context=context)), 'example.test')
        self.assertEqual(tls_host(SimpleNamespace(conn=SimpleNamespace(sni=None), context=context)), 'server.test')

    def test_ca_validation_keeps_trust_check_and_rejects_nonzero(self):
        with patch('controller.subprocess.run', return_value=SimpleNamespace(returncode=0)) as check:
            verify_ca('/test/root.pem')
            args = check.call_args.args[0]
            self.assertIn('-l', args)
            self.assertIn('-L', args)
            self.assertNotIn('-r', args)  # Never supply an explicit trust anchor to force success.
        with patch('controller.subprocess.run', return_value=SimpleNamespace(returncode=1)):
            with self.assertRaises(CertificateTrustError):
                verify_ca('/test/untrusted.pem')


if __name__ == '__main__':
    unittest.main()
