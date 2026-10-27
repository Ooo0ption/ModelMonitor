"""Full traffic archive plus the existing lightweight model statistics journal."""
import json
import os
from pathlib import Path
import sys
import time
from mitmproxy import ctx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from full_capture import FullCapture
from capture_diagnostics import tls_host, record_failure, record_success


class ModelCapture:
    def running(self):
        os.umask(0o077)
        self.root = os.environ['MODELSCOPE_DATA_DIR']
        folder = os.path.join(self.root, 'journals')
        os.makedirs(folder, exist_ok=True)
        name = 'capture-' + str(time.time_ns()) + '-' + str(os.getpid()) + '.jsonl'
        self.fd = os.open(os.path.join(folder, name), os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        self.health = {'capture_error': '', 'session': str(time.time_ns()), 'tls_hosts': {}}
        self.save_health()
        ctx.options.update(web_password=os.environ['MODELSCOPE_WEB_TOKEN'])

    def save_health(self):
        path = os.path.join(self.root, 'capture-health.json')
        with open(path + '.tmp', 'w') as stream:
            json.dump(self.health, stream, ensure_ascii=False)
        os.replace(path + '.tmp', path)

    def record_error(self, error):
        self.health['capture_error'] = 'Could not save record: ' + type(error).__name__
        self.save_health()

    def record(self, event, timestamp, flow_id, from_client, key):
        if not isinstance(event, dict):
            return
        typ = event.get('type', '')
        if from_client:
            model = event.get('model')
            if not isinstance(model, str) or not model:
                return
            clean = {'type': typ, 'model': model}
        else:
            if typ == 'codex.response.metadata':
                return
            response = event.get('response')
            if not isinstance(response, dict):
                if event.get('object') in ('response', 'chat.completion', 'chat.completion.chunk'):
                    response = event
                else:
                    return
            if not isinstance(response.get('id'), str):
                return
            fields = {k: response[k] for k in ('id', 'model', 'status', 'object') if k in response}
            if str(response.get('object', '')).startswith('chat.completion'):
                if any(isinstance(c, dict) and c.get('finish_reason') is not None for c in response.get('choices', [])):
                    fields['status'] = 'completed'
            clean = {'type': typ, 'response': fields}
        row = {'event': clean, 'time': timestamp, 'flow_id': flow_id, 'from_client': from_client, 'key': key}
        payload = (json.dumps(row, ensure_ascii=False, separators=(',', ':')) + '\n').encode()
        # Only this process writes its journal. Finish a short write before fsync.
        while payload:
            payload = payload[os.write(self.fd, payload):]
        os.fsync(self.fd)

    def websocket_message(self, flow):
        try:
            msg = flow.websocket.messages[-1]
            try:
                event = json.loads(msg.content)
            except (ValueError, UnicodeDecodeError):
                return
            index = len(flow.websocket.messages) - 1
            self.record(event, msg.timestamp, flow.id, msg.from_client, f'{flow.id}:ws:{index}:{msg.timestamp}')
        except Exception as exc:
            self.record_error(exc)

    def body_events(self, content):
        if not content:
            return
        try:
            yield json.loads(content)
            return
        except (ValueError, UnicodeDecodeError):
            pass
        body = content.decode('utf-8', 'replace') if isinstance(content, bytes) else content
        for block in body.replace('\r\n', '\n').split('\n\n'):
            data = '\n'.join(line[5:].lstrip(' ') for line in block.splitlines() if line.startswith('data:'))
            if data and data != '[DONE]':
                try:
                    yield json.loads(data)
                except ValueError:
                    continue

    def request(self, flow):
        if not any(p in flow.request.path for p in ('/responses', '/chat/completions')):
            return
        try:
            for event in self.body_events(flow.request.content):
                self.record(event, flow.request.timestamp_start, flow.id, True, f'{flow.id}:http')
        except Exception as exc:
            self.record_error(exc)

    def response(self, flow):
        if not any(p in flow.request.path for p in ('/responses', '/chat/completions')) or flow.response.status_code == 101:
            return
        try:
            for event in self.body_events(flow.response.content):
                self.record(event, flow.response.timestamp_start, flow.id, False, f'{flow.id}:http')
        except Exception as exc:
            self.record_error(exc)

    def tls_failed_client(self, data):
        record_failure(self.health, tls_host(data), getattr(data.conn, 'error', None))
        self.save_health()

    def tls_established_client(self, data):
        if record_success(self.health, tls_host(data)):
            self.save_health()

    def done(self):
        if hasattr(self, 'fd'):
            os.close(self.fd)


addons = [FullCapture(), ModelCapture()]
