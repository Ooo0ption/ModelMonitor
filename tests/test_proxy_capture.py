"""Real local mitmweb integration; uses test-only data and fresh ephemeral ports."""
import base64
import gzip
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import socketserver
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time

SRC = Path(__file__).resolve().parents[1] / 'src'
sys.path.insert(0, str(SRC))
from traffic_archive import archive_files, export_day, iter_records, date_key

UPLOAD = b'private request\x00\xff' + bytes(range(256)) * 500
RESPONSE = gzip.compress(b'private response\x00\xff' + bytes(range(256)) * 500)
TEXT = json.dumps({'type': 'response.create', 'model': 'gpt-test', 'input': 'Keep the complete prompt!'}).encode()
BINARY = b'\x00\xff\x80binary-message'
FIRST = b'data: {"type":"response.created","response":{"id":"test-stream","model":"gpt-test","object":"response"}}\n\n'
LAST = b'data: {"type":"response.completed","response":{"id":"test-stream","model":"gpt-test","status":"completed","object":"response","output":[{"text":"entire response"}]}}\n\n'
release_sse = threading.Event()


def exact(stream, n):
    data = b''
    while len(data) < n:
        part = stream.read(n - len(data))
        if not part:
            raise EOFError()
        data += part
    return data


def frame(stream):
    first, second = exact(stream, 2)
    size = second & 127
    if size == 126: size = struct.unpack('!H', exact(stream, 2))[0]
    if size == 127: size = struct.unpack('!Q', exact(stream, 8))[0]
    mask = exact(stream, 4) if second & 128 else b''
    payload = exact(stream, size)
    if mask: payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return first & 15, payload


def send_frame(stream, opcode, payload, masked=False):
    size = len(payload)
    extra = b''
    code = size
    if size >= 126: code, extra = 126, struct.pack('!H', size)
    header = bytes([128 | opcode, code | (128 if masked else 0)]) + extra
    if masked:
        mask = b'ABCD'
        header += mask
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    stream.write(header + payload); stream.flush()


class Origin(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    def log_message(self, *_): pass
    def do_POST(self):
        body = exact(self.rfile, int(self.headers.get('Content-Length', '0')))
        if self.path == '/unrelated/upload':
            assert body == UPLOAD
            self.send_response(200)
            self.send_header('Content-Encoding', 'gzip')
            self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Set-Cookie', 'first=test')
            self.send_header('Set-Cookie', 'second=test')
            self.send_header('Content-Length', str(len(RESPONSE)))
            self.end_headers(); self.wfile.write(RESPONSE); self.wfile.flush()
        elif self.path == '/v1/responses':
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Content-Length', str(len(FIRST + LAST)))
            self.end_headers(); self.wfile.write(FIRST); self.wfile.flush()
            release_sse.wait(15)
            self.wfile.write(LAST); self.wfile.flush()
    def do_GET(self):
        if self.path == '/ws':
            self.send_response(101)
            self.send_header('Upgrade', 'websocket'); self.send_header('Connection', 'Upgrade')
            key = self.headers['Sec-WebSocket-Key'] + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
            self.send_header('Sec-WebSocket-Accept', base64.b64encode(hashlib.sha1(key.encode()).digest()).decode())
            self.end_headers()
            for _ in range(3):
                opcode, data = frame(self.rfile)
                send_frame(self.wfile, opcode, data)
                if opcode == 8: break
        else:
            self.send_response(404); self.send_header('Content-Length', '0'); self.end_headers()


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0)); return s.getsockname()[1]


def run():
    with tempfile.TemporaryDirectory(prefix='trace-full-integration-') as folder:
        root = Path(folder)
        origin = ThreadingHTTPServer(('127.0.0.1', 0), Origin)
        threading.Thread(target=origin.serve_forever, daemon=True).start()
        origin_port = origin.server_port
        cert_config = root/'tls.conf'
        cert_config.write_text('[req]\nprompt=no\ndistinguished_name=dn\nx509_extensions=ext\n[dn]\nCN=localhost\n[ext]\nsubjectAltName=DNS:localhost,IP:127.0.0.1\nbasicConstraints=critical,CA:TRUE\n')
        cert, key = root/'origin.pem', root/'origin.key'
        subprocess.run(['/usr/bin/openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1',
                        '-config',str(cert_config),'-keyout',str(key),'-out',str(cert)],check=True,capture_output=True)
        tls_origin = ThreadingHTTPServer(('127.0.0.1',0),Origin)
        tls_context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls_context.load_cert_chain(cert,key)
        tls_origin.socket=tls_context.wrap_socket(tls_origin.socket,server_side=True)
        threading.Thread(target=tls_origin.serve_forever,daemon=True).start()
        class Echo(socketserver.StreamRequestHandler):
            def handle(self):
                body=exact(self.rfile,8)
                self.wfile.write(body[::-1]); self.wfile.flush()
        tcp_origin=socketserver.ThreadingTCPServer(('127.0.0.1',0),Echo)
        threading.Thread(target=tcp_origin.serve_forever,daemon=True).start()
        proxy_port, web_port = free_port(), free_port()
        data = root / 'data'; data.mkdir()
        env = dict(os.environ, MODELSCOPE_DATA_DIR=str(data), MODELSCOPE_WEB_TOKEN='test-only', PYTHONDONTWRITEBYTECODE='1')
        for key in ('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy'):
            env.pop(key, None)
        command = ['/opt/homebrew/bin/mitmweb', '--listen-host', '127.0.0.1', '--listen-port', str(proxy_port),
                   '--web-port', str(web_port), '--no-web-open-browser', '--set', 'confdir=' + str(root/'ca'),
                   '--set','ssl_verify_upstream_trusted_ca='+str(cert),
                   '-s', str(SRC/'capture_addon.py'), '-q']
        log = (root/'proxy.log').open('wb')
        proc = subprocess.Popen(command, env=env, stdout=log, stderr=log)
        def records():
            return [pair for path in archive_files(data) for pair in iter_records(path)]
        def wait_ready(previous_session=None):
            for _ in range(120):
                if proc.poll() is not None: raise RuntimeError((root/'proxy.log').read_text())
                health = data/'archive-health.json'
                if health.exists():
                    value=json.loads(health.read_text())
                    if value.get('ready') and value.get('session') != previous_session:
                        return value['session']
                time.sleep(.1)
            else: raise RuntimeError('Proxy startup timed out')
        try:
            first_session=wait_ready()
            conn = http.client.HTTPConnection('127.0.0.1', proxy_port, timeout=10)
            conn.request('POST', f'http://127.0.0.1:{origin_port}/unrelated/upload', UPLOAD,
                         {'Authorization': 'Bearer test-secret', 'Cookie':'session=test', 'Content-Type':'application/octet-stream'})
            reply = conn.getresponse()
            assert reply.read() == RESPONSE
            conn.close()
            all_records = records()
            upload_id = next(e['flow_id'] for e, _ in all_records if e.get('state',{}).get('url','').endswith('/unrelated/upload'))
            for kind, expected in [('http.request.body',UPLOAD),('http.response.body',RESPONSE)]:
                actual = b''.join(p for e,p in all_records if e.get('flow_id')==upload_id and e['kind']==kind)
                assert actual == expected, (kind,len(actual),len(expected))
            state = next(e['state'] for e,p in reversed(all_records) if e.get('flow_id')==upload_id and e['kind']=='http.response.end')
            headers = [[base64.b64decode(x['$bytes_base64']) for x in pair] for pair in state['request']['headers']]
            assert [b'Authorization',b'Bearer test-secret'] in headers
            response_headers = [[base64.b64decode(x['$bytes_base64']) for x in pair] for pair in state['response']['headers']]
            assert sum(k.lower()==b'set-cookie' for k,v in response_headers)==2
            print('HTTP binary upload, compressed response and all headers: PASS', flush=True)

            # Exercise actual failure/success hooks with a test-only untrusted CA.
            for _ in range(3):
                rejected=http.client.HTTPSConnection('127.0.0.1',proxy_port,context=ssl.create_default_context(),timeout=10)
                rejected.set_tunnel('localhost',tls_origin.server_port)
                try:
                    rejected.request('POST','/unrelated/upload',UPLOAD)
                    raise AssertionError('Untrusted test proxy unexpectedly accepted')
                except ssl.SSLCertVerificationError:
                    pass
                finally:
                    rejected.close()
            for _ in range(100):
                diagnostic=json.loads((data/'capture-health.json').read_text())
                failures=diagnostic.get('tls_hosts',{}).get('localhost',{}).get('failures',[])
                if len(failures)>=3: break
                time.sleep(.01)
            assert len(failures)>=3
            assert diagnostic['tls_hosts']['localhost']['last_error']

            context=ssl.create_default_context(cafile=str(root/'ca'/'mitmproxy-ca-cert.pem'))
            conn=http.client.HTTPSConnection('127.0.0.1',proxy_port,context=context,timeout=10)
            conn.set_tunnel('localhost',tls_origin.server_port)
            conn.request('POST','/unrelated/upload',UPLOAD,{'Content-Type':'application/octet-stream'})
            assert conn.getresponse().read()==RESPONSE
            conn.close()
            https_records=records()
            assert any(e['kind']=='tls.established_client' for e,p in https_records)
            assert any(e.get('state',{}).get('url','').startswith('https://localhost:') for e,p in https_records)
            diagnostic=json.loads((data/'capture-health.json').read_text())
            assert diagnostic['tls_hosts']['localhost']['failures']==[]
            assert diagnostic['tls_hosts']['localhost']['last_success']
            print('Per-host TLS failure diagnostics clear after a successful handshake: PASS',flush=True)
            print('HTTPS decryption with certificate validation and CONNECT metadata: PASS',flush=True)

            conn = http.client.HTTPConnection('127.0.0.1', proxy_port, timeout=15)
            conn.request('POST', f'http://127.0.0.1:{origin_port}/v1/responses', TEXT, {'Content-Type':'application/json'})
            reply = conn.getresponse(); assert reply.read(len(FIRST)) == FIRST
            assert any(p==FIRST for e,p in records() if e['kind']=='http.response.body')
            release_sse.set(); assert reply.read() == LAST; conn.close()
            print('SSE body persisted before response finished: PASS', flush=True)

            sock = socket.create_connection(('127.0.0.1',proxy_port),timeout=10)
            stream = sock.makefile('rwb', buffering=0)
            stream.write((f'GET http://127.0.0.1:{origin_port}/ws HTTP/1.1\r\nHost: 127.0.0.1:{origin_port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n').encode())
            response = b''
            while not response.endswith(b'\r\n\r\n'): response += exact(stream,1)
            assert b'101' in response.split(b'\r\n')[0]
            for opcode, payload in ((1,TEXT),(2,BINARY)):
                send_frame(stream,opcode,payload,True)
                assert frame(stream)==(opcode,payload)
            ws = [(e,p) for e,p in records() if e['kind']=='websocket.message']
            assert [p for e,p in ws]==[TEXT,TEXT,BINARY,BINARY]
            assert [e['direction'] for e,p in ws]==['client_to_server','server_to_client']*2
            print('Live WebSocket text and binary payloads in both directions: PASS', flush=True)
            send_frame(stream,8,struct.pack('!H',1000),True)
            frame(stream); stream.close(); sock.close()

            sock=socket.create_connection(('127.0.0.1',proxy_port),timeout=10)
            stream=sock.makefile('rwb',buffering=0)
            address='127.0.0.1:'+str(tcp_origin.server_address[1])
            stream.write(('CONNECT '+address+' HTTP/1.1\r\nHost: '+address+'\r\n\r\n').encode())
            headers=b''
            while not headers.endswith(b'\r\n\r\n'): headers+=exact(stream,1)
            assert b'200' in headers.split(b'\r\n')[0]
            raw=b'\x00rawdata'; stream.write(raw); assert exact(stream,len(raw))==raw[::-1]
            stream.close();sock.close()
            assert [p for e,p in records() if e['kind']=='tcp.message']==[raw,raw[::-1]]
            print('Generic TCP tunnel payloads in both directions: PASS',flush=True)
            time.sleep(.1)
            proc.terminate(); proc.wait(10)
            result=export_day(data,date_key())
            assert result['events']>10
            assert len(list(Path(result['path']).glob('*/request.body.bin')))>=2
            assert any(e['kind']=='session.stop' for e,p in records())
            assert not json.loads((data/'archive-health.json').read_text())['ready']
            print('Persistence and complete offline export after proxy shutdown: PASS', flush=True)
            ledger=[json.loads(line) for path in (data/'journals').glob('*.jsonl') for line in path.open()]
            assert any(r['event'].get('type')=='response.completed' for r in ledger)
            print('Existing model statistics still recorded: PASS', flush=True)

            saved_files={p.name:p.read_bytes() for p in archive_files(data)}
            proc=subprocess.Popen(command,env=env,stdout=log,stderr=log)
            second_session=wait_ready(first_session)
            release_sse.clear()
            conn=http.client.HTTPConnection('127.0.0.1',proxy_port,timeout=15)
            conn.request('POST',f'http://127.0.0.1:{origin_port}/v1/responses',TEXT,{'Content-Type':'application/json'})
            reply=conn.getresponse();assert reply.read(len(FIRST))==FIRST
            proc.kill();proc.wait(10);conn.close();release_sse.set()
            assert any(e['session']==second_session and p==FIRST for e,p in records())
            proc=subprocess.Popen(command,env=env,stdout=log,stderr=log)
            wait_ready(second_session)
            proc.terminate();proc.wait(10)
            for path in archive_files(data):
                if path.name in saved_files: assert path.read_bytes()==saved_files[path.name]
            result=export_day(data,date_key())
            assert result['events']>20
            print('Forced termination mid-stream, restart and preservation of previous archives: PASS',flush=True)

            bad=root/'unwritable-capture';bad.mkdir();(bad/'Captures').write_text('not a directory')
            broken=subprocess.Popen(command,env=dict(env,MODELSCOPE_DATA_DIR=str(bad)),stdout=log,stderr=log)
            assert broken.wait(15)==74
            assert json.loads((bad/'archive-health.json').read_text())['error']
            print('Archive write failure stops proxy and reports the error: PASS',flush=True)
        except Exception:
            print((root/'proxy.log').read_text()[-12000:])
            raise
        finally:
            release_sse.set()
            if proc.poll() is None: proc.terminate(); proc.wait(10)
            log.close(); origin.shutdown(); origin.server_close()
            tls_origin.shutdown(); tls_origin.server_close(); tcp_origin.shutdown(); tcp_origin.server_close()


if __name__ == '__main__':
    run()
