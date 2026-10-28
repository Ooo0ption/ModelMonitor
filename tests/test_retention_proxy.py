"""Real HTTP/SSE and mixed-model WebSocket traffic through an isolated proxy."""
import http.client
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import base64
import hashlib

from test_proxy_capture import frame,send_frame
SRC=Path(__file__).resolve().parents[1]/'src';sys.path.insert(0,str(SRC))
from traffic_archive import archive_files,iter_records


def events(model,rid):
    secret='SECRET_AST' if 'astra' in model or model=='codex-auto-review' else 'KEEP_LUNA'
    return [{'type':'response.created','response':{'id':rid,'model':model,'instructions':secret}},
            {'type':'response.output_text.delta','response_id':rid,'delta':secret*100},
            {'type':'response.completed','response':{'id':rid,'model':model,'status':'completed','output':[secret],'usage':{'total_tokens':5}}}]


class Origin(BaseHTTPRequestHandler):
    def log_message(self,*_):pass
    def do_POST(self):
        request=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        body=b''.join(b'data: '+json.dumps(v).encode()+b'\n\n' for v in events(request['model'],'http-test'))
        self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Content-Length',str(len(body)))
        self.end_headers();self.wfile.write(body)
    def do_GET(self):
        self.send_response(101);self.send_header('Upgrade','websocket');self.send_header('Connection','Upgrade')
        key=self.headers['Sec-WebSocket-Key']+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
        self.send_header('Sec-WebSocket-Accept',base64.b64encode(hashlib.sha1(key.encode()).digest()).decode());self.end_headers()
        for index in range(2):
            _,payload=frame(self.rfile);request=json.loads(payload)
            for event in events(request['model'],'ws-'+str(index)):send_frame(self.wfile,1,json.dumps(event).encode())
        opcode,payload=frame(self.rfile);send_frame(self.wfile,opcode,payload)


def run():
    with tempfile.TemporaryDirectory(prefix='mm-retention-',dir='/private/tmp') as directory:
        root=Path(directory)
        env=dict(os.environ,MODELSCOPE_DATA_DIR=str(root),MODELSCOPE_LOCATION_FILE=str(root/'location.json'))
        settings={'gpt_app':'/Applications/UnusedTestTarget.app','mitmweb':'/opt/homebrew/bin/mitmweb','ca_file':str(root/'CA/mitmproxy-ca-cert.pem'),
                  'proxy_port':18831,'web_port':18832,'web_token':'synthetic-retention','legacy_ledger':''}
        (root/'settings.json').write_text(json.dumps(settings))
        def call(command):
            result=subprocess.run([sys.executable,str(SRC/'controller.py'),command],env=env,capture_output=True,text=True,timeout=40)
            assert result.returncode==0,result.stdout+result.stderr+(root/'proxy.log').read_text() if (root/'proxy.log').exists() else result.stdout
            return json.loads(result.stdout)
        call('prepare-setup');state=call('start')['proxy']
        # Exercise the user-run activation path on an isolated profile. Mark
        # this test worker as having an older policy, then require a restart
        # on the original ports and a verified new policy.
        health_path=root/'archive-health.json';health=json.loads(health_path.read_text())
        old_session=health['session'];health.pop('storage_policy',None);health_path.write_text(json.dumps(health))
        activation=subprocess.run([sys.executable,str(SRC.parent/'tools/apply_storage_rules.py')],env=env,capture_output=True,text=True,timeout=40)
        assert activation.returncode==0,activation.stdout+activation.stderr
        fresh=call('status')['proxy'];assert fresh['proxy_port']==state['proxy_port'] and fresh['web_port']==state['web_port']
        assert json.loads(health_path.read_text())['session']!=old_session
        server=ThreadingHTTPServer(('127.0.0.1',0),Origin);threading.Thread(target=server.serve_forever,daemon=True).start()
        try:
            client=http.client.HTTPConnection('127.0.0.1',state['proxy_port'],timeout=10)
            client.request('POST','http://127.0.0.1:'+str(server.server_port)+'/v1/responses',body=json.dumps({'model':'codex-auto-review','input':'SECRET_AST_HTTP'}).encode())
            response=client.getresponse();body=response.read();client.close()
            assert b'SECRET_AST' in body  # Forwarded response is unchanged.
            sock=socket.create_connection(('127.0.0.1',state['proxy_port']),timeout=10);stream=sock.makefile('rwb')
            stream.write((f'GET http://127.0.0.1:{server.server_port}/ws HTTP/1.1\r\nHost: 127.0.0.1:{server.server_port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n').encode());stream.flush()
            assert b'101' in stream.readline()
            while stream.readline()!=b'\r\n':pass
            luna=[]
            for index,model in enumerate(['gpt-6-astra','gpt-6-luna']):
                request=json.dumps({'type':'response.create','model':model,'input':'SECRET_AST_WS' if index==0 else 'KEEP_LUNA_REQUEST'}).encode()
                send_frame(stream,1,request,masked=True)
                if index==1:luna.append(request)
                for expected in events(model,'ws-'+str(index)):
                    _,payload=frame(stream);assert json.loads(payload)==expected
                    if index==1:luna.append(payload)
            send_frame(stream,8,b'',masked=True);frame(stream);stream.close();sock.close()
            time.sleep(.2);call('stop')
            records=[r for path in archive_files(root) for r in iter_records(path)]
            payloads=[payload for event,payload in records]
            assert b'SECRET_AST' not in b''.join(payloads)
            assert all(payload in payloads for payload in luna)
            assert any(event['kind']=='http.response.summary' for event,payload in records)
            assert all(path.name.endswith('.trace.gz') for path in archive_files(root))
            report=call('status')['report'];assert report['response_models']=={'codex-auto-review':1,'gpt-6-astra':1,'gpt-6-luna':1}
            print('PASS: real proxy forwards full HTTP and WebSocket contents; Astra/auto-review archives contain summaries, Luna bytes are unchanged, all model counts remain, gzip archives validate.')
        finally:
            call('stop');server.shutdown();server.server_close()


if __name__=='__main__':run()
