#!/usr/bin/env python3
"""Read mitmweb/mitmproxy captures; retain only model metadata. Python 3.9+."""
import argparse
import collections
import csv
import datetime as dt
import getpass
import html
import json
import os
from pathlib import Path
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
DEFAULT_FILTER = '~websocket | ~u /responses | ~u /chat/completions'
MAX_RECORD = 512 * 1024 * 1024


def text(value):
    return value.decode('utf-8', 'replace') if isinstance(value, bytes) else str(value or '')


def parse_value(data, offset=0, depth=0):
    """Decode native mitmproxy tnetstrings, preserving binary content."""
    if depth > 100:
        raise ValueError('Capture nesting is too deep')
    colon = data.find(b':', offset, offset + 21)
    if colon < 0 or not data[offset:colon].isdigit():
        raise ValueError('Not a valid mitmproxy flows file. Use the binary file exported with Save.')
    end = colon + 1 + int(data[offset:colon])
    if end >= len(data):
        raise ValueError('Incomplete flows file')
    payload, tag = data[colon + 1:end], data[end:end + 1]
    if tag in (b']', b'}'):
        values, pos = [], colon + 1
        while pos < end:
            value, pos = parse_value(data, pos, depth + 1)
            values.append(value)
        if pos != end or (tag == b'}' and len(values) % 2):
            raise ValueError('Invalid flows container')
        value = values if tag == b']' else {text(k): v for k, v in zip(values[::2], values[1::2])}
    elif tag == b',':
        value = payload
    elif tag == b';':
        value = payload.decode('utf-8')
    elif tag == b'#':
        value = int(payload)
    elif tag == b'^':
        value = float(payload)
    elif tag == b'!':
        if payload not in (b'true', b'false'):
            raise ValueError('Invalid flows boolean value')
        value = payload == b'true'
    elif tag == b'~' and not payload:
        value = None
    else:
        raise ValueError('Unsupported flows data type')
    return value, end + 1


def read_exact(stream, length):
    chunks = bytearray()
    while len(chunks) < length:
        part = stream.read(length - len(chunks))
        if not part:
            raise ValueError('Incomplete flows file or download')
        chunks.extend(part)
    return bytes(chunks)


def iter_flows(stream):
    while True:
        prefix = stream.read(1)
        if not prefix:
            return
        while not prefix.endswith(b':'):
            if len(prefix) > 20 or not prefix[-1:].isdigit():
                raise ValueError('Input is not a native mitmproxy flows file')
            prefix += read_exact(stream, 1)
        size = int(prefix[:-1])
        if size > MAX_RECORD:
            raise ValueError('One flow exceeds 512 MiB. Filter the desired connections in mitmweb and export them first.')
        record = prefix + read_exact(stream, size + 1)
        flow, _ = parse_value(record)
        if not isinstance(flow, dict):
            raise ValueError('Invalid top-level flows record')
        yield flow


def empty_ledger():
    return {'version': 1, 'responses': {}, 'requests': {}}


def merge_response(ledger, response, event_type, timestamp, flow_id):
    rid = response.get('id')
    if not isinstance(rid, str) or not rid:
        return False
    records = ledger['responses']
    r = records.setdefault(rid, {'response_id': rid, 'first_seen': timestamp,
        'last_seen': timestamp, 'models': [], 'events': [], 'flow_ids': [],
        'status': 'unknown', 'status_at': 0})
    r['first_seen'] = min(r['first_seen'], timestamp)
    r['last_seen'] = max(r['last_seen'], timestamp)
    model = response.get('model')
    for key, value in [('models', model), ('events', event_type), ('flow_ids', flow_id)]:
        if isinstance(value, str) and value and value not in r[key]:
            r[key].append(value)
            r[key].sort()
    status = response.get('status') or {
        'response.created': 'in_progress', 'response.in_progress': 'in_progress',
        'response.completed': 'completed', 'response.failed': 'failed',
        'response.incomplete': 'incomplete'}.get(event_type)
    terminal = {'completed', 'failed', 'incomplete', 'cancelled'}
    newer = timestamp > r['status_at']
    same_time_progress = timestamp == r['status_at'] and (r['status'] not in terminal or status in terminal)
    if status and (newer or same_time_progress):
        r['status'], r['status_at'] = status, timestamp
    return True


def consume_event(ledger, event, timestamp, flow_id, from_client, message_key, stats):
    if not isinstance(event, dict) or not isinstance(timestamp, (int, float)):
        return
    event_type = event.get('type', '')
    if not from_client and event_type == 'codex.response.metadata':
        return
    if from_client:
        # Metadata only: no prompts, response text, headers or credentials.
        model = event.get('model')
        if isinstance(model, str) and model:
            ledger['requests'][message_key] = {'request_key': message_key,
                'time': timestamp, 'model': model, 'flow_id': flow_id}
        return
    response = event.get('response')
    if not isinstance(response, dict):
        if event.get('object') in ('response', 'chat.completion', 'chat.completion.chunk'):
            response = event
        else:
            return
    if str(response.get('object') or '').startswith('chat.completion'):
        response = dict(response)
        if any(c.get('finish_reason') is not None for c in response.get('choices', []) if isinstance(c, dict)):
            response['status'] = 'completed'
    if not merge_response(ledger, response, event_type or response.get('object', ''), timestamp, flow_id):
        stats['responses_without_id'] += 1


def json_events(content):
    """JSON or SSE bodies; join multiline SSE data fields."""
    if not content:
        return
    try:
        yield json.loads(content)
        return
    except (ValueError, TypeError, UnicodeDecodeError):
        pass
    body = text(content).replace('\r\n', '\n').replace('\r', '\n')
    for block in body.split('\n\n'):
        data = '\n'.join(line[5:].lstrip(' ') for line in block.splitlines() if line.startswith('data:'))
        if data and data != '[DONE]':
            try:
                yield json.loads(data)
            except ValueError:
                continue


def decoded_body(message):
    body = message.get('content') or b''
    encoding = next((text(v).lower() for k, v in message.get('headers', []) if text(k).lower() == 'content-encoding'), '')
    if encoding == 'gzip':
        import gzip
        return gzip.decompress(body)
    if encoding == 'deflate':
        import zlib
        return zlib.decompress(body)
    if encoding not in ('', 'identity'):
        raise ValueError('Unsupported HTTP content encoding: ' + encoding)
    return body


def ingest(ledger, flows):
    stats = collections.Counter()
    for flow in flows:
        stats['flows'] += 1
        fid = text(flow.get('id'))
        ws = flow.get('websocket')
        if isinstance(ws, dict):
            for index, message in enumerate(ws.get('messages', [])):
                stats['websocket_messages'] += 1
                if not isinstance(message, list) or len(message) < 4:
                    stats['unreadable_messages'] += 1
                    continue
                _, from_client, content, timestamp = message[:4]
                try:
                    event = json.loads(content)
                except (ValueError, TypeError, UnicodeDecodeError):
                    stats['unreadable_messages'] += 1
                    continue
                consume_event(ledger, event, timestamp, fid, from_client,
                              f'{fid}:ws:{index}:{timestamp}', stats)
        else:
            request = flow.get('request') or {}
            path = text(request.get('path'))
            if '/responses' not in path and '/chat/completions' not in path:
                continue
            for direction in ('request', 'response'):
                message = flow.get(direction)
                if not isinstance(message, dict):
                    continue
                timestamp = message.get('timestamp_start') or flow.get('timestamp_created')
                try:
                    events = list(json_events(decoded_body(message)))
                except (ValueError, OSError) as exc:
                    stats['unreadable_http_bodies'] += 1
                    continue
                for event in events:
                    consume_event(ledger, event, timestamp, fid, direction == 'request', f'{fid}:http', stats)
    return dict(stats)


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=str(path.parent), prefix='.tmp-')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_ledger(path):
    if not path.exists():
        return empty_ledger()
    ledger = json.loads(path.read_text(encoding='utf-8'))
    if ledger.get('version') != 1 or not isinstance(ledger.get('responses'), dict) or not isinstance(ledger.get('requests'), dict):
        raise ValueError('Invalid local ledger format. Stopped to preserve your records.')
    ledger.pop('metadata', None)
    return ledger
