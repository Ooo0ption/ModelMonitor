"""Lossless, append-only application traffic archives. Stdlib; Python 3.9+.

Each committed record is a length-prefixed JSON envelope, raw payload bytes,
and a SHA-256 digest. Readers can recover all complete records after a crash.
The frozen mitmproxy runtime has no sqlite3, so the capture writer uses files.
"""
import base64
import datetime as dt
import hashlib
import gzip
import html
import json
import os
from pathlib import Path
import struct
import time
import zlib

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
    def __init__(self, root, session=None, rotate_bytes=ROTATE_BYTES, compress=False):
        self.root = Path(root) / 'Captures'
        self.session = session or str(time.time_ns()) + '-' + str(os.getpid())
        self.rotate_bytes = rotate_bytes
        self.fd = None
        self.day = None
        self.segment = 0
        self.size = 0
        self.sequence = 0
        self.seen = set()
        self.compress = compress
        self.compressor = None

    def _write_bytes(self, data):
        if self.compressor is not None:
            data = self.compressor.compress(data)
        self._write_stored(data)

    def _write_stored(self, data):
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
        if self.compressor is not None:
            self._write_stored(self.compressor.flush(zlib.Z_SYNC_FLUSH))
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
            self.path = folder / (self.session + '-%06d.trace' % self.segment + ('.gz' if self.compress else ''))
            self.fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            self.compressor = zlib.compressobj(3, zlib.DEFLATED, 31) if self.compress else None
            self.day, self.size, self.seen = day, 0, set()
            self._write_bytes(MAGIC)
        fid = event.get('flow_id')
        if fid and fid not in self.seen and context is not None:
            self._record({'kind': 'flow.context', 'time': timestamp, 'flow_id': fid, 'state': context}, b'')
            self.seen.add(fid)
        self._record(event, payload)

    def close(self):
        if self.fd is not None:
            if self.compressor is not None:
                self._write_stored(self.compressor.flush(zlib.Z_FINISH))
                self.compressor = None
            os.fsync(self.fd)
            os.close(self.fd)
            self.fd = None


def iter_records(path, warnings=None):
    """Yield committed events. A truncated tail is reported, never fabricated."""
    def incomplete():
        if warnings is not None:
            warnings.append(str(path) + ': incomplete final record (active write or interrupted capture)')

    compressed = str(path).endswith('.gz')
    opener = gzip.open if compressed else open
    with opener(path, 'rb') as stream:
        def read_available(size):
            # GzipFile.read() can discard a partial result when a live stream
            # has no footer yet. read1() preserves that prefix for validation.
            if not compressed:
                return stream.read(size)
            chunks = []; remaining = size
            while remaining:
                try:
                    block = stream.read1(min(remaining, 1024 * 1024))
                except EOFError:
                    break
                if not block: break
                chunks.append(block); remaining -= len(block)
            return b''.join(chunks)

        if read_available(len(MAGIC)) != MAGIC:
            raise ValueError('Unsupported traffic archive: ' + str(path))
        end = None if compressed else os.fstat(stream.fileno()).st_size
        while end is None or stream.tell() < end:
            try:
                header = read_available(HEADER.size)
            except EOFError:
                # gzip has no final footer while the writer is live. Every
                # yielded record still had its own complete SHA-256 checksum.
                return
            except (OSError, zlib.error) as error:
                raise ValueError('Archive compression/checksum mismatch: '+str(path)) from error
            if not header: return
            if len(header) != HEADER.size:
                incomplete(); return
            metadata_size, payload_size = HEADER.unpack(header)
            if end is not None and metadata_size + payload_size + 32 > end - stream.tell():
                incomplete(); return
            try:
                metadata = read_available(metadata_size)
                payload = read_available(payload_size)
                checksum = read_available(32)
            except EOFError:
                incomplete(); return
            except (OSError, zlib.error) as error:
                raise ValueError('Archive compression/checksum mismatch: '+str(path)) from error
            if len(metadata)!=metadata_size or len(payload)!=payload_size or len(checksum)!=32:
                incomplete(); return
            digest = hashlib.sha256(metadata)
            digest.update(payload)
            if digest.digest() != checksum:
                raise ValueError('Archive checksum mismatch: ' + str(path))
            yield json.loads(metadata), payload


def archive_files(root, day=None):
    folder = Path(root) / 'Captures'
    if day is not None:
        day = dt.date.fromisoformat(day).isoformat()
        return sorted([*(folder / day).glob('*.trace'), *(folder / day).glob('*.trace.gz')])
    return sorted([*folder.glob('*/*.trace'), *folder.glob('*/*.trace.gz')])


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
                    flows[fid] = {'folder': folder, 'state': {}, 'time': event['time'], 'messages': 0, 'summaries': 0,
                                  'request_complete': False, 'response_complete': False, 'ended': False}
                info = flows[fid]
                kind = event['kind']
                if event.get('retention') == 'summary': info['summaries'] += 1
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
                    if event.get('retention') != 'summary': (info['folder'] / (side + '.body.bin')).touch(exist_ok=True)
                if kind in ('http.request.summary', 'http.response.summary'):
                    side = kind.split('.')[1]
                    (info['folder'] / (side + '.summary.json')).write_bytes(payload)
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
            summary = info['folder'] / (side + '.summary.json')
            if summary.exists():
                parts.append('<p>Summary-only retention: the original body was intentionally omitted.</p><p><a href="'+summary.name+'">Saved packet summary</a></p>')
            body = info['folder'] / (side + '.body.bin')
            if body.exists():
                parts.append('<p><a href="' + body.name + '" download>Complete body (' + str(body.stat().st_size) + ' bytes)</a></p>')
                with body.open('rb') as stream:
                    preview = stream.read(16384)
                parts.append('<p><small>First 16 KiB preview; saved bytes retain any Content-Encoding.</small></p><pre>' + html.escape(preview.decode('utf-8', 'replace')) + '</pre>')
        if info['messages']:
            parts.append('<h2>Messages</h2><p><a href="messages.jsonl">All ' + str(info['messages']) + ' saved messages and summaries (JSONL)</a></p>')
            if info['summaries']: parts.append('<p>Entries marked summary contain metadata rather than the original prompt, output or tool content.</p>')
            with (info['folder'] / 'messages.jsonl').open(encoding='utf-8') as stream:
                for i, line in enumerate(stream):
                    if i >= 20:
                        break
                    record = json.loads(line)
                    payload = base64.b64decode(record.pop('payload_base64'))
                    parts.append('<details><summary>' + html.escape(record['kind'] + ' / ' + record.get('direction', '')) + '</summary><pre>' + html.escape(payload[:16384].decode('utf-8', 'replace')) + '</pre></details>')
            parts.append('<small>Preview: first 20 entries, up to 16 KiB each. The linked file contains every retained entry.</small>')
        (info['folder'] / 'state.json').write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
        parts.append('<p><a href="state.json">Complete saved connection and flow metadata</a></p>')
        (info['folder'] / 'index.html').write_text(_page(title, ''.join(parts)), encoding='utf-8')
        stamp = dt.datetime.fromtimestamp(info['time'], ZONE).isoformat(timespec='milliseconds')
        rows.append('<tr><td>' + html.escape(stamp) + '</td><td><a href="' + info['folder'].name + '/index.html">' + html.escape(title) + '</a></td><td>' + str(info['messages']) + '</td></tr>')
    notes = ('<p>Retained flows with events on ' + day + ' (Hong Kong), including saved portions from other dates. '
             'Full entries may include credentials; summary entries deliberately omit original bodies. This export is local and does not contact the captured hosts.</p>'
             '<p><a href="events.jsonl">All saved events, payloads and summaries (JSONL)</a></p>'
             '<p>HTTP wire framing and WebSocket control frames are not exposed by mitmproxy. Missing final events indicate an unfinished or interrupted flow.</p>')
    if warnings:
        notes += '<pre>' + html.escape('\n'.join(sorted(set(warnings)))) + '</pre>'
    (output / 'index.html').write_text(_page('Saved traffic — ' + day, notes + '<table><tr><th>Time</th><th>Flow</th><th>Messages</th></tr>' + ''.join(rows) + '</table>'), encoding='utf-8')
    (output / 'manifest.json').write_text(json.dumps({'date': day, 'events': count, 'flows': len(flows), 'warnings': sorted(set(warnings))}, indent=2), encoding='utf-8')
    for path in output.rglob('*'):
        os.chmod(path, 0o700 if path.is_dir() else 0o600)
    return {'path': str(output), 'index': str(output / 'index.html'), 'events': count, 'flows': len(flows), 'warnings': len(set(warnings))}
