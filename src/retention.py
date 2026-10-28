"""Model-specific retention. Unknown or ambiguous traffic remains lossless."""
import collections
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import time
import zlib

from traffic_archive import ArchiveWriter, HEADER, MAGIC, dumps, iter_records, _text

POLICY = {'version': 1, 'summary_models': ['Astra family', 'codex-auto-review'], 'other_models': 'full', 'unknown_models': 'full', 'compression':'gzip'}
TERMINAL = {'response.completed', 'response.failed', 'response.incomplete', 'response.cancelled'}
PROBE_LIMIT = 16 * 1024 * 1024


def summary_model(model):
    if not isinstance(model, str): return False
    name = model.lower()
    return name == 'codex-auto-review' or re.fullmatch(r'(?:gpt-\d+(?:\.\d+)*-)?astra(?:-\d{4}-\d{2}-\d{2})?', name) is not None


def numeric_tree(value):
    if isinstance(value, dict):
        return {k: numeric_tree(v) for k, v in value.items() if isinstance(v, (dict, int, float)) and not isinstance(v, bool)}
    return value


def safe_event(value):
    result = {k: value[k] for k in ('type', 'model', 'response_id', 'item_id', 'sequence_number', 'output_index', 'content_index', 'summary_index') if k in value}
    response = value.get('response')
    if not isinstance(response, dict) and value.get('object') in ('response', 'chat.completion', 'chat.completion.chunk'):
        response = value
    if isinstance(response, dict):
        clean = {k: response[k] for k in ('id', 'object', 'model', 'status', 'created_at', 'completed_at') if k in response}
        if isinstance(response.get('usage'), dict): clean['usage'] = numeric_tree(response['usage'])
        if isinstance(response.get('error'), dict):
            clean['error'] = {k: response['error'][k] for k in ('type', 'code') if k in response['error']}
        result['response'] = clean
    item = value.get('item')
    if isinstance(item, dict): result['item'] = {k: item[k] for k in ('id', 'type', 'role', 'status') if k in item}
    return result


def summary_context(context):
    if not context: return context
    result = {k: context[k] for k in ('id', 'type', 'timestamp_created') if k in context}
    result['retention'] = 'summary'
    if context.get('url'): result['url'] = context['url'].split('?', 1)[0]
    for side in ('request', 'response'):
        message = context.get(side)
        if isinstance(message, dict):
            result[side] = {k: message[k] for k in ('method', 'scheme', 'host', 'port', 'http_version', 'status_code', 'timestamp_start', 'timestamp_end') if k in message}
            if message.get('path'): result[side]['path'] = _text(message['path']).split('?', 1)[0]
            result[side]['headers'] = [[_text(k), _text(v)] for k, v in message.get('headers', []) if _text(k).lower() in ('content-type', 'content-encoding', 'content-length')]
    return result


class Pending:
    """Fsync undecided records before forwarding; recover them as full on restart."""
    def __init__(self, root):
        folder = Path(root)/'Pending'; folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = folder/(secrets.token_hex(16)+'.trace')
        self.file = self.path.open('xb', buffering=0); self.file.write(MAGIC)
        self.started = time.time(); self.size = 0

    def add(self, event, payload, context):
        envelope = dumps({'record': event, 'context': context}).encode()
        digest = hashlib.sha256(envelope); digest.update(payload)
        for part in (HEADER.pack(len(envelope),len(payload)), envelope, payload, digest.digest()):
            view = memoryview(part)
            while view:
                count = self.file.write(view)
                if not count: raise OSError('Pending record write did not advance')
                view = view[count:]
        os.fsync(self.file.fileno()); self.size += len(payload)

    def records(self):
        for wrapper, payload in iter_records(self.path): yield wrapper['record'], payload, wrapper.get('context')

    def remove(self):
        self.file.close(); self.path.unlink()


class Probe:
    def __init__(self, encoding=''):
        self.buffer = b''; self.failed = encoding not in ('', 'identity', 'gzip', 'deflate')
        self.decoder = zlib.decompressobj(31 if encoding == 'gzip' else 15) if encoding in ('gzip','deflate') else None
        self.model = None; self.value = {}; self.events = 0

    def accept(self, data):
        try: value = json.loads(data)
        except (ValueError, UnicodeDecodeError): return
        if not isinstance(value, dict): return
        response = value.get('response')
        response = response if isinstance(response, dict) else value
        model = response.get('model')
        if isinstance(model, str) and model: self.model = model
        if isinstance(response.get('id'), str) or model: self.value = safe_event(value)
        self.events += 1

    def feed(self, payload, final=False):
        if self.failed: return
        try:
            data = self.decoder.decompress(payload, PROBE_LIMIT+1) if self.decoder else payload
            self.buffer += data
            if len(self.buffer) > PROBE_LIMIT or (self.decoder and self.decoder.unconsumed_tail):
                self.failed = True; self.buffer = b''; return
            while True:
                split = re.search(br'\r?\n\r?\n', self.buffer)
                if not split: break
                block = self.buffer[:split.start()]; self.buffer = self.buffer[split.end():]
                lines = [line[5:].lstrip(b' ') for line in block.splitlines() if line.startswith(b'data:')]
                if lines: self.accept(b'\n'.join(lines))
            if final and self.buffer:
                lines = [line[5:].lstrip(b' ') for line in self.buffer.splitlines() if line.startswith(b'data:')]
                self.accept(b'\n'.join(lines) if lines else self.buffer); self.buffer = b''
        except (zlib.error, ValueError): self.failed = True; self.buffer = b''


class SelectiveArchiveWriter:
    def __init__(self, root, session=None, known=None, offline=False):
        self.root = Path(root); self.writer = ArchiveWriter(root, session, compress=True); self.session = self.writer.session
        self.pending = {}; self.ambiguous = set(); self.responses = {}; self.active = {}; self.items = {}
        self.http = {}; self.known = known or {}; self.closed = False; self.offline = offline
        # A crashed process may leave a request whose eventual model was never
        # identified. Preserve it, rather than assuming it belonged to Astra.
        for path in sorted((self.root/'Pending').glob('*.trace')):
            warnings = []
            for wrapper, payload in iter_records(path, warnings):
                event = dict(wrapper['record'], retention='full_unclassified', recovered_pending=True)
                self.writer.write(event, payload, wrapper.get('context'))
            if warnings: self.writer.write({'kind':'retention.pending_recovery_warning','time':time.time(),'warning_count':len(warnings)})
            path.unlink()

    def raw(self, event, payload=b'', context=None): self.writer.write(event, payload, context)

    def summarized(self, event, value, payload, context, extra=None):
        clean = safe_event(value)
        clean['_capture_summary'] = dict({'policy':'summary','original_bytes':len(payload),'sha256':hashlib.sha256(payload).hexdigest()}, **(extra or {}))
        self.raw(dict(event, retention='summary'), dumps(clean).encode(), context)

    def flush_pending(self, fid, model=None, rid=None):
        waiting = self.pending.pop(fid, [])
        keep_summary = len(waiting) == 1 and summary_model(model) and fid not in self.ambiguous
        for queue in waiting:
            for event, payload, context in queue.records():
                if keep_summary:
                    try: value = json.loads(payload)
                    except (ValueError, UnicodeDecodeError): self.raw(event,payload,context); continue
                    if not isinstance(value,dict): self.raw(event,payload,context); continue
                    if event.get('direction') == 'server_to_client' and rid: value = dict(value, response_id=rid)
                    self.summarized(event,value,payload,context,{'response_model':model})
                else: self.raw(event,payload,context)
            queue.remove()

    def websocket(self, event, payload, context):
        fid = event['flow_id']
        try: value = json.loads(payload)
        except (ValueError, UnicodeDecodeError): self.raw(event,payload,context); return
        if not isinstance(value,dict): self.raw(event,payload,context); return
        typ = value.get('type',''); typ = typ if isinstance(typ,str) else ''
        from_client = event.get('direction') == 'client_to_server'
        if from_client and (typ == 'response.create' or not typ) and isinstance(value.get('model'),str):
            key = f"{fid}:ws:{event['message_index']}:{event['time']}"
            if key in self.known.get('requests', {}):
                model = self.known['requests'][key]
                if summary_model(model): self.summarized(event,value,payload,context,{'response_model':model})
                else: self.raw(event,payload,context)
            elif fid in self.ambiguous: self.raw(event,payload,context)
            else:
                queue = Pending(self.root); queue.add(event,payload,context)
                self.pending.setdefault(fid,[]).append(queue)
                if len(self.pending[fid]) > 1:
                    self.ambiguous.add(fid); self.flush_pending(fid)
            return
        if typ == 'codex.response.metadata' and len(self.pending.get(fid,[])) == 1:
            self.pending[fid][0].add(event,payload,context); return
        response = value.get('response')
        response = response if isinstance(response,dict) else (value if value.get('object') in ('response','chat.completion','chat.completion.chunk') else {})
        rid = response.get('id') or value.get('response_id')
        item = value.get('item') if isinstance(value.get('item'),dict) else {}
        item_id = value.get('item_id') or item.get('id')
        if not isinstance(rid,str): rid = None
        if not isinstance(item_id,str): item_id = None
        rid = rid or self.items.get((fid,item_id)) or self.known.get('items',{}).get((fid,item_id))
        if not rid and typ.startswith('response.') and len(self.active.get(fid,set())) == 1 and not self.pending.get(fid):
            rid = next(iter(self.active[fid]))
        if not rid: self.raw(event,payload,context); return
        model = response.get('model') or self.known.get('responses',{}).get((fid,rid))
        key = (fid,rid)
        if key not in self.responses:
            self.flush_pending(fid,model,rid)
            self.responses[key] = {'model':model,'count':0,'bytes':0,'digest':hashlib.sha256(),'types':collections.Counter(),'first':event['time'],'last':event['time'],'value':{},'event':event,'context':context,'finished':False}
        record = self.responses[key]
        if isinstance(model,str): record['model'] = model
        if item_id: self.items[(fid,item_id)] = rid
        for output in response.get('output') or []:
            if isinstance(output,dict) and isinstance(output.get('id'),str): self.items[(fid,output['id'])] = rid
        self.active.setdefault(fid,set()).add(rid)
        record['count'] += 1; record['bytes'] += len(payload); record['digest'].update(payload); record['types'][typ] += 1; record['last'] = event['time']
        if response: record['value'] = safe_event(value)
        record['event'] = event; record['context'] = context
        terminal = typ in TERMINAL
        if summary_model(record['model']):
            if typ == 'response.created' or item_id and typ in ('response.output_item.added','response.output_item.done'):
                self.summarized(event,dict(value,response_id=rid),payload,context)
            if terminal: self.finish_response(fid,rid)
        else: self.raw(event,payload,context)
        if terminal:
            record['finished'] = True; self.active[fid].discard(rid)

    def finish_response(self, fid, rid, interrupted=False):
        record = self.responses[(fid,rid)]
        if record['finished'] or not summary_model(record['model']): return
        value = dict(record['value'])
        value.setdefault('response',{'id':rid,'model':record['model']})
        value['response_id'] = rid
        if interrupted: value['type'] = 'capture.response_summary'
        extra = {'policy':'summary','original_bytes':record['bytes'],'event_count':record['count'],
                 'event_types':dict(record['types']),'first_event':record['first'],'last_event':record['last'],
                 'sha256_of_concatenated_payloads':record['digest'].hexdigest(),'interrupted':interrupted}
        value['_capture_summary'] = extra
        self.raw(dict(record['event'],retention='summary'),dumps(value).encode(),record['context'])
        record['finished'] = True

    def write(self, event, payload=b'', context=None):
        kind = event['kind']; fid = event.get('flow_id')
        if event.get('retention') == 'summary': self.raw(event,payload,context); return
        if kind == 'session.stop' and not self.offline: self.flush()
        if kind == 'websocket.message': self.websocket(event,payload,context); return
        if kind == 'websocket.end':
            self.flush_pending(fid)
            for response_id in list(self.active.get(fid,set())): self.finish_response(fid,response_id,True)
        if kind == 'http.request.headers':
            state = context or event.get('state') or {}; request = state.get('request') or {}
            path = _text(request.get('path')); method = _text(request.get('method'))
            if method in ('POST','PUT','PATCH') and any(part in path for part in ('/responses','/chat/completions','/conversation')):
                self.http[fid] = HttpRetention(self,fid,self.known.get('http',{}).get(fid))
        if fid in self.http and kind.startswith('http.'):
            self.http[fid].feed(event,payload,context)
            if kind in ('http.response.end','http.error'): self.http.pop(fid,None)
            return
        self.raw(event,payload,context)

    def flush(self):
        for fid in list(self.pending): self.flush_pending(fid)
        for fid,rid in list(self.responses): self.finish_response(fid,rid,True)
        for value in list(self.http.values()): value.finish()
        self.http.clear()

    def close(self):
        if not self.closed: self.flush(); self.writer.close(); self.closed = True


class HttpRetention:
    def __init__(self, owner, fid, known_model=None):
        self.owner=owner; self.fid=fid; self.queue=Pending(owner.root); self.mode=None; self.probes={}
        self.sizes=collections.Counter(); self.hashes={'request':hashlib.sha256(),'response':hashlib.sha256()}
        self.last_event={}; self.context=None; self.known_model=known_model

    def summary(self, side, event, context):
        probe=self.probes.get(side); value=dict(probe.value) if probe else {}
        if side=='request': value.setdefault('type','request.summary')
        value['_capture_summary']={'policy':'summary','original_bytes':self.sizes[side],'sha256':self.hashes[side].hexdigest(),
                                   'parsed_events':probe.events if probe else 0,'response_model':(self.probes.get('response').model if self.probes.get('response') else None) or self.known_model}
        new=dict(event,kind='http.'+side+'.summary',retention='summary'); new.pop('state',None)
        self.owner.raw(new,dumps(value).encode(),summary_context(context))

    def commit(self,event,payload,context):
        if self.mode=='full': self.owner.raw(event,payload,context); return
        kind=event['kind']
        if kind.endswith('.body'): return
        clean=dict(event,retention='summary'); safe=summary_context(context or event.get('state'))
        if 'state' in clean: clean['state']=safe
        if kind in ('http.request.end','http.response.end'): self.summary(kind.split('.')[1],event,context)
        if kind=='http.error': self.summary('response',event,context)
        self.owner.raw(clean,b'',safe)

    def decide(self,mode):
        self.mode=mode
        if self.queue:
            for event,payload,context in self.queue.records(): self.commit(event,payload,context)
            self.queue.remove(); self.queue=None

    def feed(self,event,payload,context):
        kind=event['kind'];self.last_event=event;self.context=context
        side=kind.split('.')[1] if kind.count('.')>=2 else None
        if kind.endswith('.headers') and side in ('request','response'):
            message=(context or event.get('state') or {}).get(side) or {}
            encoding=next((_text(v).lower() for k,v in message.get('headers',[]) if _text(k).lower()=='content-encoding'),'')
            self.probes[side]=Probe(encoding)
        if kind.endswith('.body') and side in ('request','response'):
            self.sizes[side]+=len(payload);self.hashes[side].update(payload)
            self.probes.setdefault(side,Probe()).feed(payload)
        if kind.endswith('.end') and side in self.probes: self.probes[side].feed(b'',True)
        if self.mode is None:
            self.queue.add(event,payload,context)
            probe=self.probes.get('response')
            model = (probe.model if probe else None) or self.known_model
            if model: self.decide('summary' if summary_model(model) else 'full')
            elif kind in ('http.response.end','http.error') or probe and probe.failed: self.decide('full')
        else: self.commit(event,payload,context)

    def finish(self):
        if self.mode is None: self.decide('full')
        elif self.mode=='summary' and self.last_event.get('kind') not in ('http.response.end','http.error'):
            self.summary('response',self.last_event,self.context)
