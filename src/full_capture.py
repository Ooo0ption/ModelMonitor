"""Capture proxied traffic with model-specific payload retention."""
import json
import os
import secrets
from pathlib import Path
import sys
import time

from mitmproxy import ctx
from retention import SelectiveArchiveWriter, POLICY


class FullCapture:
    def __init__(self):
        self.writer = None
        self.offsets = {}

    def running(self):
        os.umask(0o077)
        self.root = Path(os.environ['MODELSCOPE_DATA_DIR'])
        self.health = {'archive_version': 1, 'ready': False, 'error': ''}
        try:
            worker_session = os.environ.get('MODELSCOPE_CAPTURE_SESSION')
            # A script can be loaded again in the same proxy process. Give
            # each recorder a new archive namespace, preserving existing files.
            archive_session = (worker_session or str(time.time_ns())) + '-' + secrets.token_hex(6)
            self.writer = SelectiveArchiveWriter(self.root, archive_session)
            self.writer.write({'kind': 'session.start', 'time': time.time(),
                               'scope': 'Astra and codex-auto-review summaries; other and unclassified traffic retained in full',
                               'storage_policy': POLICY})
            ctx.options.update(store_streamed_bodies=True)
            self.health.update(ready=True, session=worker_session or self.writer.session,
                               archive_session=self.writer.session, storage_policy=POLICY)
            self.save_health()
        except Exception as exc:
            self.fatal(exc)

    def save_health(self):
        path = self.root / 'archive-health.json'
        with (self.root / 'archive-health.json.tmp').open('w') as stream:
            json.dump(self.health, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(self.root / 'archive-health.json.tmp', path)

    def fatal(self, exc):
        message = 'Full traffic recording failed; proxy stopped to prevent unrecorded traffic: ' + str(exc)
        self.health.update(ready=False, error=message)
        try:
            self.save_health()
        except Exception:
            pass
        try:
            sys.stderr.write(message + '\n')
            sys.stderr.flush()
        except OSError:
            pass
        # Every preceding archive record was fsynced. Continuing to forward after
        # an ENOSPC/IO failure would silently violate the full-capture contract.
        os._exit(74)

    def snapshot(self, flow):
        state = {'id': flow.id, 'type': flow.type, 'timestamp_created': flow.timestamp_created,
                 'client_conn': flow.client_conn.get_state(), 'server_conn': flow.server_conn.get_state(),
                 'error': flow.error.get_state() if flow.error else None,
                 'metadata': flow.metadata, 'comment': flow.comment, 'is_replay': flow.is_replay}
        for side in ('request', 'response'):
            message = getattr(flow, side, None)
            if message is not None:
                data = message.get_state()
                # HTTP bodies have their own incremental byte records. DNS
                # state remains intact because DNS has no HTTP content field.
                if flow.type == 'http':
                    data.pop('content', None)
                state[side] = data
        request = getattr(flow, 'request', None)
        if flow.type == 'http' and request:
            state['url'] = request.pretty_url
        ws = getattr(flow, 'websocket', None)
        if ws is not None:
            state['websocket'] = {key: getattr(ws, key) for key in ('closed_by_client', 'close_code', 'close_reason', 'timestamp_end')}
        return state

    def emit(self, kind, flow=None, payload=b'', timestamp=None, state=False, **fields):
        try:
            event = dict(fields, kind=kind, time=time.time() if timestamp is None else timestamp)
            context = None
            if flow is not None:
                event['flow_id'] = flow.id
                context = self.snapshot(flow)
                if state:
                    event['state'] = context
            self.writer.write(event, payload, context)
        except Exception as exc:
            self.fatal(exc)

    def stream(self, flow, side):
        key = (flow.id, side)
        self.offsets[key] = 0

        def tee(chunk):
            if chunk:
                offset = self.offsets.get(key, 0)
                self.emit('http.' + side + '.body', flow, chunk, offset=offset)
                self.offsets[key] = offset + len(chunk)
            return chunk

        getattr(flow, side).stream = tee

    def requestheaders(self, flow):
        self.emit('http.request.headers', flow, state=True)
        self.stream(flow, 'request')

    def responseheaders(self, flow):
        self.emit('http.response.headers', flow, state=True)
        self.stream(flow, 'response')

    def finished(self, flow, side):
        key = (flow.id, side)
        # Synthetic/replayed messages may not have passed a streaming callback.
        if key not in self.offsets:
            content = getattr(flow, side).raw_content
            if content:
                self.emit('http.' + side + '.body', flow, content, offset=0)
        self.emit('http.' + side + '.end', flow, state=True)
        self.offsets.pop(key, None)

    def request(self, flow):
        self.finished(flow, 'request')

    def response(self, flow):
        self.finished(flow, 'response')

    def error(self, flow):
        self.emit('http.error', flow, state=True)
        self.offsets.pop((flow.id, 'request'), None)
        self.offsets.pop((flow.id, 'response'), None)

    def http_connect(self, flow):
        self.emit('http.connect', flow, state=True)

    def http_connected(self, flow):
        self.emit('http.connected', flow, state=True)

    def http_connect_error(self, flow):
        self.emit('http.connect_error', flow, state=True)

    def websocket_start(self, flow):
        self.emit('websocket.start', flow, state=True)

    def websocket_message(self, flow):
        msg = flow.websocket.messages[-1]
        self.emit('websocket.message', flow, msg.content, msg.timestamp,
                  direction='client_to_server' if msg.from_client else 'server_to_client',
                  opcode=int(msg.type), dropped=msg.dropped, injected=msg.injected,
                  message_index=len(flow.websocket.messages) - 1)

    def websocket_end(self, flow):
        self.emit('websocket.end', flow, state=True)

    def transport_message(self, flow):
        msg = flow.messages[-1]
        self.emit(flow.type + '.message', flow, msg.content, msg.timestamp,
                  direction='client_to_server' if msg.from_client else 'server_to_client',
                  message_index=len(flow.messages) - 1)

    def tcp_start(self, flow): self.emit('tcp.start', flow, state=True)
    def tcp_message(self, flow): self.transport_message(flow)
    def tcp_end(self, flow): self.emit('tcp.end', flow, state=True)
    def tcp_error(self, flow): self.emit('tcp.error', flow, state=True)
    def udp_start(self, flow): self.emit('udp.start', flow, state=True)
    def udp_message(self, flow): self.transport_message(flow)
    def udp_end(self, flow): self.emit('udp.end', flow, state=True)
    def udp_error(self, flow): self.emit('udp.error', flow, state=True)
    def dns_request(self, flow): self.emit('dns.request', flow, state=True)
    def dns_response(self, flow): self.emit('dns.response', flow, state=True)
    def dns_error(self, flow): self.emit('dns.error', flow, state=True)

    def client_connected(self, client):
        self.emit('connection.client_connected', flow_id='connection:' + client.id, connection=client.get_state())

    def client_disconnected(self, client):
        self.emit('connection.client_disconnected', flow_id='connection:' + client.id, connection=client.get_state())

    def connection_event(self, kind, data):
        self.emit(kind, flow_id='connection:' + data.server.id,
                  server=data.server.get_state(), client=data.client.get_state())

    def server_connected(self, data): self.connection_event('connection.server_connected', data)
    def server_disconnected(self, data): self.connection_event('connection.server_disconnected', data)
    def server_connect_error(self, data): self.connection_event('connection.server_error', data)

    def tls_event(self, kind, data):
        self.emit(kind, flow_id='connection:' + data.conn.id, connection=data.conn.get_state())

    def tls_established_client(self, data): self.tls_event('tls.established_client', data)
    def tls_established_server(self, data): self.tls_event('tls.established_server', data)
    def tls_failed_client(self, data): self.tls_event('tls.failed_client', data)
    def tls_failed_server(self, data): self.tls_event('tls.failed_server', data)

    def done(self):
        if self.writer is not None:
            self.emit('session.stop')
            self.writer.close()
            self.health['ready'] = False
            self.save_health()
