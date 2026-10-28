"""Local proxy regression: immutable scripts, live reload and fail-closed unload."""
import http.client
import http.cookiejar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import urllib.error


def run():
    with tempfile.TemporaryDirectory(prefix='monitor-lifecycle-',dir='/private/tmp') as tmp:
        root=Path(tmp);source=root/'source';profile=root/'profile';profile.mkdir()
        shutil.copytree(Path(__file__).resolve().parents[1]/'src',source)
        env=dict(os.environ,MODELSCOPE_DATA_DIR=str(profile),MODELSCOPE_LOCATION_FILE=str(root/'location.json'))
        settings={'gpt_app':'/Applications/UnusedTestTarget.app','mitmweb':'/opt/homebrew/bin/mitmweb',
                  'ca_file':str(profile/'CertificateAuthority/mitmproxy-ca-cert.pem'),
                  'proxy_port':18835,'web_port':18836,'web_token':'synthetic-lifecycle-test','legacy_ledger':''}
        (profile/'settings.json').write_text(json.dumps(settings))
        def call(command):
            result=subprocess.run([sys.executable,str(source/'controller.py'),command],env=env,capture_output=True,text=True,timeout=45)
            if result.returncode:
                raise AssertionError(result.stdout+result.stderr+(profile/'proxy.log').read_text())
            return json.loads(result.stdout)
        call('prepare-setup')
        state=call('start')['proxy']
        class Origin(BaseHTTPRequestHandler):
            def log_message(self,*_):pass
            def do_GET(self):
                self.send_response(200);self.end_headers();self.wfile.write(b'synthetic lifecycle response')
        origin=ThreadingHTTPServer(('127.0.0.1',0),Origin)
        threading.Thread(target=origin.serve_forever,daemon=True).start()
        cookies=http.cookiejar.CookieJar()
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPCookieProcessor(cookies))
        base='http://127.0.0.1:'+str(state['web_port'])
        def api(path,data=None):
            headers={'Authorization':'Bearer '+settings['web_token']}
            if data is not None:
                headers.update({'Content-Type':'application/json','X-XSRFToken':next(c.value for c in cookies if c.name=='_mitmproxy_xsrf')})
            request=urllib.request.Request(base+path,headers=headers,data=json.dumps(data).encode() if data is not None else None,method='PUT' if data is not None else 'GET')
            with opener.open(request,timeout=5) as response:return response.read()
        try:
            options=json.loads(api('/options'));script=Path(options['scripts']['value'][0])
            assert script.parent.parent==profile/'Runtime'
            (source/'capture_addon.py').unlink()
            time.sleep(1.2)
            client=http.client.HTTPConnection('127.0.0.1',state['proxy_port'],timeout=10)
            client.request('GET','http://127.0.0.1:'+str(origin.server_port)+'/survived-update')
            response=client.getresponse();assert response.read()==b'synthetic lifecycle response';client.close()
            assert call('status')['proxy']['recording']
            # Obtain the normal XSRF cookie, then remove/reload in one quick
            # pair as an app update used to do. No real traffic or profile.
            try:api('/updates')
            except urllib.error.HTTPError as error:
                assert error.code==400  # WebSocket upgrade omitted; XSRF cookie is set.
            first=json.loads((profile/'archive-health.json').read_text())['archive_session']
            api('/options',{'scripts':[]});api('/options',{'scripts':[str(script)]})
            for _ in range(20):
                health=json.loads((profile/'archive-health.json').read_text())
                if health.get('ready') and health.get('archive_session')!=first:break
                time.sleep(.1)
            assert health['ready'] and health['archive_session']!=first
            assert call('status')['proxy']['recording']
            api('/options',{'scripts':[]})
            for _ in range(40):
                if not call('status')['proxy']['running']:break
                time.sleep(.1)
            assert not call('status')['proxy']['running']
            assert 'stopped to prevent' in json.loads((profile/'archive-health.json').read_text())['error']
            print('PASS: source replacement does not unload capture; reload preserves existing archives; disabled recorder stops the managed proxy.')
        finally:
            call('stop');origin.shutdown();origin.server_close()


if __name__=='__main__':run()
