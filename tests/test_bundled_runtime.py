"""Smoke-test a built app with a private profile; never changes Keychain trust."""
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
from traffic_archive import archive_files, iter_records


def run(app):
    resources = Path(app).resolve()/'Contents/Resources'
    helper = resources/'Helper/model-monitor-helper'
    with tempfile.TemporaryDirectory(prefix='model-monitor-bundle-check-') as tmp:
        profile = Path(tmp)
        env = dict(os.environ, MODELSCOPE_DATA_DIR=str(profile), MODELSCOPE_LOCATION_FILE=str(profile/'location.json'),
                   PATH='/usr/bin:/bin:/usr/sbin:/sbin', PYINSTALLER_RESET_ENVIRONMENT='1')
        settings = {'gpt_app':'/Applications/UnusedTestTarget.app',
                    'mitmweb':str(profile/'old-install/mitmweb'),
                    'ca_file':str(profile/'CertificateAuthority/mitmproxy-ca-cert.pem'),
                    'proxy_port':18831, 'web_port':18832, 'web_token':'synthetic-local-test', 'legacy_ledger':''}
        (profile/'settings.json').write_text(json.dumps(settings))
        def call(command):
            result = subprocess.run([str(helper),command],env=env,capture_output=True,text=True,timeout=60)
            if result.returncode:
                log=profile/'proxy.log'
                diagnostic=log.read_text(errors='replace')[-6000:] if log.exists() else ''
                raise AssertionError(result.stdout[-2000:]+result.stderr[-2000:]+diagnostic)
            value=json.loads(result.stdout)
            assert value['ok'],value
            return value
        status=call('status')
        assert status['dependencies']['bundled_proxy']
        assert status['settings']['mitmweb']==str(resources/'mitmproxy.app/Contents/MacOS/mitmweb')
        assert not status['dependencies']['certificate']
        prepared=call('prepare-setup')
        assert not prepared['certificate_trusted']
        key=Path(prepared['certificate_path']).parent/'mitmproxy-ca.pem'
        digest=hashlib.sha256(key.read_bytes()).digest()
        call('prepare-setup')
        assert hashlib.sha256(key.read_bytes()).digest()==digest
        assert key.stat().st_mode & 0o777 == 0o600

        class Origin(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                body=self.rfile.read(int(self.headers['Content-Length']))
                assert body==b'synthetic request'
                self.send_response(200);self.end_headers();self.wfile.write(b'bundled runtime response')
        origin=ThreadingHTTPServer(('127.0.0.1',0),Origin)
        threading.Thread(target=origin.serve_forever,daemon=True).start()
        started=False
        try:
            state=call('start')['proxy'];started=True
            assert state['running'] and state['archive_version']==1
            client=http.client.HTTPConnection('127.0.0.1',state['proxy_port'],timeout=15)
            client.request('POST','http://127.0.0.1:'+str(origin.server_port)+'/package-check',body=b'synthetic request')
            response=client.getresponse();assert response.status==200
            assert response.read()==b'bundled runtime response';client.close()
        finally:
            if started:call('stop')
            origin.shutdown();origin.server_close()
        records=[item for archive in archive_files(profile) for item in iter_records(archive)]
        assert b'synthetic request' in b''.join(body for event,body in records if event['kind']=='http.request.body')
        assert b'bundled runtime response' in b''.join(body for event,body in records if event['kind']=='http.response.body')
        assert not call('status')['proxy']['running']
        print('PASS: bundled helper/proxy, moved install recovery, new local CA, key permissions, idempotent setup, HTTP recording, worker shutdown. Keychain trust was not changed.')


if __name__=='__main__':
    run(sys.argv[1])
