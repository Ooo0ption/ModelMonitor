"""Lossless, append-only application traffic archives. Stdlib; Python 3.9+.

Each committed record is a length-prefixed JSON envelope, raw payload bytes,
and a SHA-256 digest. Readers can recover all complete records after a crash.
The frozen mitmproxy runtime has no sqlite3, so the capture writer uses files.
"""
import base64
import datetime as dt
import hashlib
import html
import json
import os
from pathlib import Path
import struct
import time

MAGIC = b'GPTTRACE\x01\n'
HEADER = struct.Struct('>IQ')
ZONE = dt.timezone(dt.timedelta(hours=8))
ROTATE_BYTES = 64 * 1024 * 1024


def encode_bytes(value):
    if isinstance(value, (bytes, bytearray)):
        return {'$bytes_base64': base64.b64encode(value).decode('ascii')}
    raise TypeError('Unsupported archive value: ' + type(value).__name__)


def decode_bytes(value):
    if isinstance(value, dict) and set(value) == {'$bytes_base64'}:
        return base64.b64decode(value['$bytes_base64'])
    return value


def dumps(value):
    return json.dumps(value, ensure_ascii=False, default=encode_bytes, separators=(',', ':'))


def date_key(timestamp=None):
    return dt.datetime.fromtimestamp(timestamp if timestamp is not None else time.time(), ZONE).date().isoformat()


class ArchiveWriter:
    def __init__(self, root, session=None, rotate_bytes=ROTATE_BYTES):
        self.root = Path(root) / 'Captures'
        self.session = session or str(time.time_ns()) + '-' + str(os.getpid())
        self.rotate_bytes = rotate_bytes
        self.fd = None
        self.day = None
        self.segment = 0
        self.size = 0
        self.sequence = 0
        self.seen = set()

    def _write_bytes(self, data):
        remaining = memoryview(data)
        while remaining:
            count = os.write(self.fd, remaining)
            if count <= 0:
                raise OSError('Archive write did not advance')
            remaining = remaining[count:]
        self.size += len(data)

    def _record(self, event, payload):
        self.sequence += 1
        envelope = dict(event, archive_version=1, session=self.session, sequence=self.sequence)
        encoded = dumps(envelope).encode('utf-8')
        digest = hashlib.sha256(encoded)
        digest.update(payload)
        self._write_bytes(HEADER.pack(len(encoded), len(payload)))
        self._write_bytes(encoded)
        self._write_bytes(payload)
        self._write_bytes(digest.digest())
        os.fsync(self.fd)

    def write(self, event, payload=b'', context=None):
        timestamp = event.setdefault('time', time.time())
        day = date_key(timestamp)
        if self.fd is None or day != self.day or self.size >= self.rotate_bytes:
            self.close()
            folder = self.root / day
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self.root, 0o700)
            os.chmod(folder, 0o700)
            self.segment += 1
            self.path = folder / (self.session + '-%06d.trace' % self.segment)
            self.fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            self.day, self.size, self.seen = day, 0, set()
            self._write_bytes(MAGIC)
        fid = event.get('flow_id')
        if fid and fid not in self.seen and context is not None:
            self._record({'kind': 'flow.context', 'time': timestamp, 'flow_id': fid, 'state': context}, b'')
            self.seen.add(fid)
        self._record(event, payload)

    def close(self):
        if self.fd is not None:
            os.fsync(self.fd)
            os.close(self.fd)
            self.fd = None


def iter_records(path, warnings=None):
    """Yield committed events. A truncated tail is reported, never fabricated."""
    def incomplete():
        if warnings is not None:
            warnings.append(str(path) + ': incomplete final record (active write or interrupted capture)')

    with Path(path).open('rb') as stream:
        if stream.read(len(MAGIC)) != MAGIC:
            raise ValueError('Unsupported traffic archive: ' + str(path))
        end = os.fstat(stream.fileno()).st_size
        while stream.tell() < end:
            header = stream.read(HEADER.size)
            if len(header) != HEADER.size:
                incomplete(); return
            metadata_size, payload_size = HEADER.unpack(header)
            if metadata_size + payload_size + 32 > end - stream.tell():
                incomplete(); return
            metadata = stream.read(metadata_size)
            payload = stream.read(payload_size)
            checksum = stream.read(32)
            digest = hashlib.sha256(metadata)
            digest.update(payload)
            if digest.digest() != checksum:
                raise ValueError('Archive checksum mismatch: ' + str(path))
            yield json.loads(metadata), payload


def archive_files(root, day=None):
    folder = Path(root) / 'Captures'
    if day is not None:
        day = dt.date.fromisoformat(day).isoformat()
        return sorted((folder / day).glob('*.trace'))
    return sorted(folder.glob('*/*.trace'))


def summary(root, day):
    files = archive_files(root, day)
    return {'date': day, 'files': len(files), 'bytes': sum(p.stat().st_size for p in files)}


def _text(value):
    if isinstance(value, dict):
        value = decode_bytes(value)
    return value.decode('utf-8', 'replace') if isinstance(value, bytes) else str(value or '')


def _page(title, content):
    return ('<!doctype html><html lang="en"><meta charset="utf-8">'
            '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'">'
            '<title>' + html.escape(title) + '</title><style>'
            'body{font:15px system-ui;max-width:1100px;margin:40px auto;padding:0 24px;color:#243b52}'
            'table{border-collapse:collapse;width:100%}td,th{padding:10px;border-bottom:1px solid #ddd;text-align:left;overflow-wrap:anywhere}'
            'pre{background:#f3f6f8;padding:16px;white-space:pre-wrap;overflow-wrap:anywhere}'
            'a{color:#126a90}small{color:#607080}</style><h1>' + html.escape(title) + '</h1>' + content + '</html>')


def export_day(root, day):
    """Export complete flows touching the chosen day, including cross-day data."""
    os.umask(0o077)
    day = dt.date.fromisoformat(day).isoformat()
    selected = archive_files(root, day)
    if not selected:
        raise ValueError('No full traffic archives for this date. Older versions only saved model metadata.')
    wanted, warnings = set(), []
    for path in selected:
        for event, _ in iter_records(path, warnings):
            if event.get('flow_id'):
                wanted.add(event['flow_id'])
    output = Path(root) / 'Exports' / day / ('traffic-' + str(time.time_ns()))
    output.mkdir(parents=True, mode=0o700)
    flows, count = {}, 0
    with (output / 'events.jsonl').open('w', encoding='utf-8') as all_events:
        for path in archive_files(root):
            for event, payload in iter_records(path, warnings):
                fid = event.get('flow_id')
                if fid not in wanted and path not in selected:
                    continue
                count += 1
                record = dict(event, payload_base64=base64.b64encode(payload).decode('ascii'))
                all_events.write(dumps(record) + '\n')
                if not fid:
                    continue
                if fid not in flows:
                    dirname = hashlib.sha256(fid.encode()).hexdigest()[:24]
                    folder = output / dirname
                    folder.mkdir(mode=0o700)
                    flows[fid] = {'folder': folder, 'state': {}, 'time': event['time'], 'messages': 0,
                                  'request_complete': False, 'response_complete': False, 'ended': False}
                info = flows[fid]
                kind = event['kind']
                if event.get('state'):
                    info['state'].update(event['state'])
                for key in ('connection', 'client', 'server'):
                    if key in event:
                        info['state'][key] = event[key]
                if kind in ('http.request.body', 'http.response.body'):
                    side = kind.split('.')[1]
                    body = info['folder'] / (side + '.body.bin')
                    actual = body.stat().st_size if body.exists() else 0
                    if actual != event.get('offset', actual):
                        warnings.append(fid + ': ' + side + ' body has a gap; consult events.jsonl')
                    with body.open('ab') as stream:
                        stream.write(payload)
                if kind in ('http.request.end', 'http.response.end'):
                    side = kind.split('.')[1]
                    info[side + '_complete'] = True
                    (info['folder'] / (side + '.body.bin')).touch(exist_ok=True)
                if kind.endswith('.end'):
                    info['ended'] = True
                if kind in ('websocket.message', 'tcp.message', 'udp.message'):
                    info['messages'] += 1
                    if kind == 'websocket.message' and event.get('opcode') == 1:
                        record['payload_text'] = payload.decode('utf-8', 'replace')
                    with (info['folder'] / 'messages.jsonl').open('a', encoding='utf-8') as stream:
                        stream.write(dumps(record) + '\n')
    rows = []
    for fid, info in sorted(flows.items(), key=lambda item: item[1]['time']):
        state = info['state']
        title = state.get('url') or fid
        parts = ['<p><a href="../index.html">All flows</a></p><p>Flow: ' + html.escape(fid) + '</p>']
        for side in ('request', 'response'):
            message = state.get(side)
            if message is None:
                continue
            friendly = dict(message)
            for key in ('method', 'scheme', 'path', 'http_version', 'reason', 'authority'):
                if key in friendly:
                    friendly[key] = _text(friendly[key])
            for key in ('headers', 'trailers'):
                if friendly.get(key) is not None:
                    friendly[key] = [[_text(k), _text(v)] for k, v in friendly[key]]
            (info['folder'] / (side + '.json')).write_text(json.dumps(friendly, ensure_ascii=False, indent=2), encoding='utf-8')
            parts.append('<h2>' + side.title() + '</h2><p>Body finished: ' + str(info[side + '_complete']) + '</p>')
            parts.append('<pre>' + html.escape(json.dumps(friendly, ensure_ascii=False, indent=2)) + '</pre>')
            body = info['folder'] / (side + '.body.bin')
            if body.exists():
                parts.append('<p><a href="' + body.name + '" download>Complete body (' + str(body.stat().st_size) + ' bytes)</a></p>')
                with body.open('rb') as stream:
                    preview = stream.read(16384)
                parts.append('<p><small>First 16 KiB preview; saved bytes retain any Content-Encoding.</small></p><pre>' + html.escape(preview.decode('utf-8', 'replace')) + '</pre>')
        if info['messages']:
            parts.append('<h2>Messages</h2><p><a href="messages.jsonl">All ' + str(info['messages']) + ' complete messages (JSONL, base64 payloads)</a></p>')
            with (info['folder'] / 'messages.jsonl').open(encoding='utf-8') as stream:
                for i, line in enumerate(stream):
                    if i >= 20:
                        break
                    record = json.loads(line)
                    payload = base64.b64decode(record.pop('payload_base64'))
                    parts.append('<details><summary>' + html.escape(record['kind'] + ' / ' + record.get('direction', '')) + '</summary><pre>' + html.escape(payload[:16384].decode('utf-8', 'replace')) + '</pre></details>')
            parts.append('<small>Preview: first 20 messages, up to 16 KiB each. The linked file contains every complete payload.</small>')
        (info['folder'] / 'state.json').write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
        parts.append('<p><a href="state.json">Complete saved connection and flow metadata</a></p>')
        (info['folder'] / 'index.html').write_text(_page(title, ''.join(parts)), encoding='utf-8')
        stamp = dt.datetime.fromtimestamp(info['time'], ZONE).isoformat(timespec='milliseconds')
        rows.append('<tr><td>' + html.escape(stamp) + '</td><td><a href="' + info['folder'].name + '/index.html">' + html.escape(title) + '</a></td><td>' + str(info['messages']) + '</td></tr>')
    notes = ('<p>Full flows with events on ' + day + ' (Hong Kong), including saved portions from other dates. '
             'Bodies, credentials and messages are included. This export is local and does not contact the captured hosts.</p>'
             '<p><a href="events.jsonl">All events and complete payloads (JSONL)</a></p>'
             '<p>HTTP wire framing and WebSocket control frames are not exposed by mitmproxy. Missing final events indicate an unfinished or interrupted flow.</p>')
    if warnings:
        notes += '<pre>' + html.escape('\n'.join(sorted(set(warnings)))) + '</pre>'
    (output / 'index.html').write_text(_page('Saved traffic — ' + day, notes + '<table><tr><th>Time</th><th>Flow</th><th>Messages</th></tr>' + ''.join(rows) + '</table>'), encoding='utf-8')
    (output / 'manifest.json').write_text(json.dumps({'date': day, 'events': count, 'flows': len(flows), 'warnings': sorted(set(warnings))}, indent=2), encoding='utf-8')
    for path in output.rglob('*'):
        os.chmod(path, 0o700 if path.is_dir() else 0o600)
    return {'path': str(output), 'index': str(output / 'index.html'), 'events': count, 'flows': len(flows), 'warnings': len(set(warnings))}
