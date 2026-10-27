"""Resolve a model activity row to its own saved HTTP exchange or WS messages."""
import datetime as dt
import codecs
import hashlib
import json
import os
from pathlib import Path
import zlib

from traffic_archive import archive_files, iter_records, dumps, _text, ZONE

PREVIEW_BYTES = 64 * 1024


def format_json_prefix(text, limit=PREVIEW_BYTES):
    """Indent a JSON prefix without requiring its truncated closing tokens.

    Keep strings, escapes, numbers and duplicate keys exactly as captured. Do
    not invent closing braces or repair incomplete values. Bound the formatted
    output too, since indentation can expand a small input considerably.
    """
    parts, used = [], 0
    depth = 0
    quoted = escaped = pending_line = False
    previous = ''

    def emit(value):
        nonlocal used
        data = value.encode('utf-8')
        if used + len(data) > limit:
            parts.append(data[:max(0, limit - used)].decode('utf-8', 'ignore'))
            return False
        parts.append(value)
        used += len(data)
        return True

    for char in text:
        if quoted:
            if not emit(char): return ''.join(parts), True
            if escaped: escaped = False
            elif char == '\\': escaped = True
            elif char == '"': quoted = False
            continue
        if char.isspace(): continue
        if char in '}]':
            depth = max(0, depth - 1)
            if previous not in ('{', '[') and not emit('\n' + '  ' * min(depth, 128)):
                return ''.join(parts), True
            pending_line = False
        elif pending_line:
            if not emit('\n' + '  ' * min(depth, 128)): return ''.join(parts), True
            pending_line = False
        if not emit(': ' if char == ':' else char): return ''.join(parts), True
        if char in '{[':
            depth += 1
            pending_line = True
        elif char == ',': pending_line = True
        elif char == '"': quoted = True
        previous = char
    return ''.join(parts), False


def text_preview(sample, truncated, size):
    try:
        # A byte limit may cut a multibyte character. Hold that incomplete tail
        # instead of misclassifying the entire UTF-8 document as binary.
        decoder = codecs.getincrementaldecoder('utf-8')()
        text = decoder.decode(sample[:PREVIEW_BYTES], final=not truncated)
        if text.lstrip().startswith(('{', '[')):
            text, formatted_cut = format_json_prefix(text)
            truncated = truncated or formatted_cut
        return text, truncated
    except UnicodeDecodeError:
        return 'Binary content (hex preview):\n' + sample[:4096].hex(' '), size > 4096


def record_info(store, mode, record_id):
    if mode == 'requests':
        row = store.db.execute('SELECT time,model,flow FROM requests WHERE key=?', (record_id,)).fetchone()
        if row:
            stamp, model, fid = row
            return {'id': record_id, 'timestamp': stamp, 'model': model, 'status': 'Sent'}, {fid}
    elif mode == 'responses':
        row = store.db.execute('SELECT first,status FROM responses WHERE id=?', (record_id,)).fetchone()
        if row:
            stamp, status = row
            models = [r[0] for r in store.db.execute('SELECT model FROM models WHERE response_id=? ORDER BY model', (record_id,))]
            flows = {r[0] for r in store.db.execute('SELECT flow_id FROM response_flows WHERE response_id=?', (record_id,))}
            return {'id': record_id, 'timestamp': stamp, 'model': '; '.join(models) or 'Model not reported', 'status': status}, flows
    raise ValueError('This model record is no longer available.')


class MessageLinks:
    """Use response/item IDs. Pair requests only when one request is pending.

    Ambiguous concurrent requests are deliberately not paired by nearest time.
    ID-less stream events are attached only while exactly one response is active.
    """
    def __init__(self):
        self.requests = {}
        self.responses = {}
        self.pending = {}
        self.active = {}
        self.items = {}
        self.unassigned = []

    def add(self, fid, ref, key, from_client, event):
        if not isinstance(event, dict):
            return
        typ = event.get('type', '')
        typ = typ if isinstance(typ, str) else ''
        if from_client:
            if isinstance(event.get('model'), str) and (typ == 'response.create' or not typ):
                self.requests[key] = {'ref': ref, 'responses': [], 'flow_id': fid}
                self.pending.setdefault(fid, []).append(key)
            return
        response = event.get('response')
        if not isinstance(response, dict):
            response = event if event.get('object') in ('response', 'chat.completion', 'chat.completion.chunk') else {}
        rid = response.get('id') or event.get('response_id')
        rid = rid if isinstance(rid, str) else None
        item = event.get('item') if isinstance(event.get('item'), dict) else {}
        item_id = event.get('item_id') or item.get('id')
        item_id = item_id if isinstance(item_id, str) else None
        rid = rid or self.items.get((fid, item_id))
        if not rid and not item_id and typ.startswith('response.'):
            active = self.active.get(fid, set())
            if len(active) == 1 and not self.pending.get(fid):
                rid = next(iter(active))
        if not isinstance(rid, str) or not rid:
            if typ.startswith('response.'):
                self.unassigned.append((fid, ref, item_id))
            return
        if rid not in self.responses:
            waiting = self.pending.get(fid, [])
            request_key = waiting.pop(0) if len(waiting) == 1 else None
            self.responses[rid] = {'refs': [], 'request_key': request_key, 'flow_id': fid}
            if request_key:
                self.requests[request_key]['responses'].append(rid)
        self.responses[rid]['refs'].append(ref)
        self.active.setdefault(fid, set()).add(rid)
        if item_id:
            self.items[(fid, item_id)] = rid
        for output in response.get('output') or []:
            if isinstance(output, dict) and isinstance(output.get('id'), str):
                self.items[(fid, output['id'])] = rid
        if typ in ('response.completed', 'response.failed', 'response.incomplete', 'response.cancelled'):
            self.active[fid].discard(rid)

    def finish(self):
        remaining = []
        for fid, ref, item_id in self.unassigned:
            rid = self.items.get((fid, item_id))
            if rid in self.responses:
                self.responses[rid]['refs'].append(ref)
            else:
                remaining.append((fid, ref, item_id))
        self.unassigned = remaining

    def select(self, mode, record_id):
        self.finish()
        request_key = record_id if mode == 'requests' else self.responses.get(record_id, {}).get('request_key')
        rids = self.requests.get(request_key, {}).get('responses', []) if mode == 'requests' else [record_id]
        refs = {}
        if request_key in self.requests:
            refs[self.requests[request_key]['ref']] = 'request'
        for rid in rids:
            for ref in self.responses.get(rid, {}).get('refs', []):
                refs[ref] = 'response'
        return refs, request_key, rids


def journal_records(root):
    for path in sorted((Path(root) / 'journals').glob('*.jsonl')):
        with path.open(encoding='utf-8') as stream:
            for number, line in enumerate(stream):
                if not line.endswith('\n'):
                    continue
                row = json.loads(line)
                yield (path.name, number), row


def packet_detail(root, store, mode, record_id):
    os.umask(0o077)
    root = Path(root)
    info, fids = record_info(store, mode, record_id)
    journals = list(journal_records(root))
    # Backfill location information for records made before response_flows existed.
    for ref, row in journals:
        response = row['event'].get('response') or {}
        if (mode == 'responses' and response.get('id') == record_id) or (mode == 'requests' and row.get('key') == record_id):
            fids.add(row['flow_id'])
    files = archive_files(root)
    links = MessageLinks()
    states, ws_flows = {}, set()
    warnings = []
    raw_found = False
    # Only envelopes and correlation references survive this pass, not all bodies
    # from a potentially long-lived WebSocket connection.
    for path in files:
        for event, payload in iter_records(path, warnings):
            fid = event.get('flow_id')
            if fids and fid not in fids:
                continue
            if event.get('state'):
                states[fid] = event['state']
            if fid in fids:
                raw_found = True
            if event['kind'] == 'websocket.message':
                ws_flows.add(fid)
                try:
                    obj = json.loads(payload)
                except (ValueError, UnicodeDecodeError):
                    continue
                ref = (event['session'], event['sequence'])
                key = f"{fid}:ws:{event['message_index']}:{event['time']}"
                links.add(fid, ref, key, event['direction'] == 'client_to_server', obj)
    chosen, request_key, rids = links.select(mode, record_id)
    if not fids and mode == 'responses' and record_id in links.responses:
        fids.add(links.responses[record_id]['flow_id'])
        raw_found = True
    is_ws = bool(ws_flows & fids)
    output = root / 'Exports' / dt.datetime.fromtimestamp(info['timestamp'], ZONE).date().isoformat() / ('packet-' + hashlib.sha256((mode + ':' + record_id).encode()).hexdigest()[:24])
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    messages = []

    def add_path(label, direction, timestamp, path, encoding=''):
        size = path.stat().st_size
        with path.open('rb') as stream:
            sample = stream.read(PREVIEW_BYTES + 1)
        original_sample = sample
        truncated = size > PREVIEW_BYTES
        note = ''
        if encoding in ('gzip', 'deflate'):
            try:
                decoder = zlib.decompressobj(31 if encoding == 'gzip' else zlib.MAX_WBITS)
                with path.open('rb') as stream:
                    sample = b''
                    while len(sample) <= PREVIEW_BYTES:
                        block = stream.read(65536)
                        if not block:
                            break
                        sample += decoder.decompress(block, PREVIEW_BYTES + 1 - len(sample))
                        if decoder.eof:
                            break
                truncated = len(sample) > PREVIEW_BYTES or not decoder.eof
                note = 'Decoded preview; the file preserves original compressed bytes.\n\n'
            except zlib.error:
                sample = original_sample
                truncated = size > PREVIEW_BYTES
                note = 'Compressed bytes could not be decoded.\n\n'
        elif encoding:
            note = 'Content-Encoding: ' + encoding + '. The file contains the complete encoded body.\n\n'
        text, truncated = text_preview(sample, truncated, size)
        messages.append({'id': path.name, 'label': label, 'direction': direction,
                         'time': dt.datetime.fromtimestamp(timestamp, ZONE).isoformat(timespec='milliseconds'),
                         'byte_count': size, 'text': note + text, 'truncated': truncated, 'path': str(path)})

    def add_bytes(label, direction, timestamp, payload, filename):
        path = output / filename
        path.write_bytes(payload)
        os.chmod(path, 0o600)
        add_path(label, direction, timestamp, path)

    notice = ''
    if raw_found:
        bodies, body_times, body_sizes = {}, {}, {}
        for path in files:
            for event, payload in iter_records(path):
                fid = event.get('flow_id')
                if fid not in fids:
                    continue
                kind = event['kind']
                if kind == 'websocket.message':
                    ref = (event['session'], event['sequence'])
                    if ref in chosen:
                        try:
                            obj = json.loads(payload)
                            label = obj.get('type', 'WebSocket message') if isinstance(obj, dict) else 'WebSocket message'
                        except (ValueError, UnicodeDecodeError):
                            label = 'WebSocket message'
                        add_bytes(label, chosen[ref], event['time'], payload,
                                  hashlib.sha256(repr(ref).encode()).hexdigest()[:24] + '.json')
                elif not is_ws and kind in ('http.request.body', 'http.response.body'):
                    side = kind.split('.')[1]
                    body_key = (fid, side)
                    if body_key not in bodies:
                        body = output / (hashlib.sha256(fid.encode()).hexdigest()[:12] + '-' + side + '.body.bin')
                        body.write_bytes(b'')
                        bodies[body_key] = body
                        body_times[body_key] = event['time']
                        body_sizes[body_key] = 0
                    if body_sizes[body_key] != event.get('offset', body_sizes[body_key]):
                        warnings.append('Some body chunks are missing; the saved body may be incomplete.')
                    with bodies[body_key].open('ab') as stream:
                        stream.write(payload)
                    body_sizes[body_key] += len(payload)
        for fid in sorted(fids):
            state = states.get(fid, {})
            for side in ('request', 'response'):
                message = state.get(side)
                if message is None:
                    continue
                friendly = dict(message)
                for key in ('method', 'scheme', 'path', 'http_version', 'reason', 'authority'):
                    if key in friendly: friendly[key] = _text(friendly[key])
                for key in ('headers', 'trailers'):
                    if friendly.get(key) is not None:
                        friendly[key] = [[_text(k), _text(v)] for k, v in friendly[key]]
                name = hashlib.sha256(fid.encode()).hexdigest()[:12] + '-' + side + '.headers.json'
                add_bytes(('WebSocket handshake / ' if is_ws else '') + side.title() + ' headers',
                          'connection' if is_ws else side, info['timestamp'], dumps(friendly).encode(), name)
                if (fid, side) in bodies:
                    encoding = next((v.lower() for k, v in friendly.get('headers', []) if k.lower() == 'content-encoding'), '')
                    add_path(side.title() + ' body', side, body_times[(fid, side)], bodies[(fid, side)], encoding)
            add_bytes('Connection metadata', 'connection', info['timestamp'], dumps(state).encode(),
                      hashlib.sha256(fid.encode()).hexdigest()[:12] + '-connection.json')
        if is_ws and not chosen:
            notice = 'The connection archive exists, but this exact model event was not found. Only connection details are shown.'
        elif is_ws and mode == 'responses' and not request_key:
            notice = 'The response is identified by its ID. Its request could not be linked unambiguously; no unrelated request is shown.'
        elif is_ws and mode == 'requests' and not rids:
            notice = 'The request is saved. A matching response has not been captured or cannot be linked unambiguously yet.'
        if links.unassigned and is_ws:
            notice += ' Stream events without enough identifying information remain available in the full connection export.'
    else:
        fallback = MessageLinks()
        selected_rows = []
        for ref, row in journals:
            if row['flow_id'] in fids:
                fallback.add(row['flow_id'], ref, row['key'], row['from_client'], row['event'])
                selected_rows.append((ref, row))
        refs, _, _ = fallback.select(mode, record_id)
        for ref, row in selected_rows:
            if ref in refs:
                add_bytes('Saved metadata / ' + row['event'].get('type', 'event'), refs[ref], row['time'],
                          dumps(row['event']).encode(), hashlib.sha256(repr(ref).encode()).hexdigest()[:24] + '.json')
        notice = 'Only model metadata was saved for this record. The complete request and response bodies are unavailable.'
        if not messages:
            add_bytes('Saved record', 'request' if mode == 'requests' else 'response', info['timestamp'], dumps(info).encode(), 'record.json')
    if warnings:
        notice += ' ' + ' '.join(sorted(set(warnings)))
    for path in output.iterdir():
        if path.is_file(): os.chmod(path, 0o600)
    info['time'] = dt.datetime.fromtimestamp(info.pop('timestamp'), ZONE).isoformat(timespec='milliseconds')
    return {'record': info, 'protocol_label': ('WebSocket' if is_ws else 'HTTP') if raw_found else 'Saved metadata',
            'url': next((states[fid].get('url', '') for fid in sorted(fids) if fid in states), ''),
            'notice': notice.strip(), 'messages': messages, 'flow_ids': sorted(fids)}
