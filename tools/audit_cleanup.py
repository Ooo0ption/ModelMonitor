#!/usr/bin/env python3
"""Verify recovery payloads already exist in the canonical traffic archives."""
import argparse
import collections
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from model_report import iter_flows, text
from traffic_archive import archive_files, iter_records
from compact_captures import digest


def manifest(folder):
    folder = Path(folder).resolve()
    result = []
    for path in sorted(folder.rglob('*')):
        if path.is_symlink():
            raise ValueError('Refusing a cleanup tree containing a symbolic link: ' + str(path))
        if path.is_file():
            result.append({'relative':str(path.relative_to(folder)), 'bytes':path.stat().st_size, 'sha256':digest(path)})
    return result


def audit(source, backups, output):
    source = Path(source).resolve(); output = Path(output)
    wanted_ws = {}; wanted_body = {}; wanted_flows = set(); candidates = []
    def ws(fid, index, stamp, body, direction):
        key = (fid, index, stamp, direction)
        checksum = hashlib.sha256(body).hexdigest()
        wanted_ws.setdefault(key, set()).add(checksum)
    def body(fid, kind, start, payload):
        if not payload: return
        key = (int(start), len(payload), hashlib.sha256(payload).hexdigest())
        wanted_body.setdefault((fid,kind), {})[key] = {'position':int(start), 'digest':hashlib.sha256()}
    for parent in backups:
        parent = Path(parent).resolve()
        files = manifest(parent)
        candidate = {'path':str(parent),'files':files,'bytes':sum(f['bytes'] for f in files),'trace_events':0,'snapshot_flows':0}
        candidates.append(candidate)
        # Explicitly verify native snapshots, including an earlier partial
        # snapshot's byte ranges, rather than assuming backups are duplicates.
        for metadata in sorted(parent.glob('snapshot.json')):
            meta = json.loads(metadata.read_text()); snapshot = Path(meta['path']).resolve()
            if parent not in snapshot.parents:
                raise ValueError('Snapshot is outside its recovery directory.')
            if digest(snapshot) != meta['sha256']:
                raise ValueError('Snapshot checksum mismatch: '+str(snapshot))
            with snapshot.open('rb') as stream:
                for flow in iter_flows(stream):
                    candidate['snapshot_flows'] += 1
                    fid = text(flow.get('id')); wanted_flows.add(fid)
                    if text(flow.get('type')) != 'http':
                        raise ValueError('A non-HTTP native snapshot requires separate inspection before removal.')
                    for side in ('request','response'):
                        message = flow.get(side) or {}
                        payload = message.get('content')
                        if isinstance(payload,bytes): body(fid,'http.'+side+'.body',0,payload)
                    for index,message in enumerate((flow.get('websocket') or {}).get('messages',[])):
                        opcode,client,payload,stamp = message[:4]
                        ws(fid,index,stamp,payload,'client_to_server' if client else 'server_to_client')
        for path in sorted(parent.rglob('*.trace')) + sorted(parent.rglob('*.trace.gz')):
            warnings = []
            for event,payload in iter_records(path,warnings):
                candidate['trace_events'] += 1
                fid = event.get('flow_id');kind=event['kind']
                if fid:wanted_flows.add(fid)
                if kind=='websocket.message':ws(fid,event['message_index'],event['time'],payload,event['direction'])
                elif kind in ('http.request.body','http.response.body'):body(fid,kind,event.get('offset',0),payload)
                elif payload:raise ValueError('A recovery archive contains an unsupported payload type: '+kind)
            if warnings:raise ValueError('Incomplete backup archive: '+str(path))
    original_ws = sum(len(v) for v in wanted_ws.values())
    flow_ids=set(); source_files=[]; warnings=[]
    for path in archive_files(source):
        source_files.append({'relative':str(path.relative_to(source)), 'bytes':path.stat().st_size, 'sha256':digest(path)})
        for event,payload in iter_records(path,warnings):
            fid=event.get('flow_id');kind=event['kind']
            if fid:flow_ids.add(fid)
            if kind=='websocket.message':
                key=(fid,event['message_index'],event['time'],event['direction'])
                if key in wanted_ws:wanted_ws[key].discard(hashlib.sha256(payload).hexdigest())
            elif (fid,kind) in wanted_body:
                start=event.get('offset',0);end=start+len(payload)
                for (offset,length,checksum),state in wanted_body[(fid,kind)].items():
                    position=state['position'];stop=offset+length
                    if start<=position<end and position<stop:
                        until=min(end,stop);state['digest'].update(payload[position-start:until-start]);state['position']=until
    missing_ws=sum(len(v) for v in wanted_ws.values())
    body_failures=[];body_verified=0
    for (fid,kind),ranges in wanted_body.items():
        for (offset,length,checksum),state in ranges.items():
            if state['position']!=offset+length or state['digest'].hexdigest()!=checksum:
                body_failures.append({'flow_id':fid,'kind':kind,'offset':offset,'length':length})
            else:body_verified+=1
    missing_flows=sorted(wanted_flows-flow_ids)
    metadata_checks=[];metadata_missing=0
    database=source/'records.sqlite3'
    if database.exists():
        db=sqlite3.connect('file:'+str(database)+'?mode=ro',uri=True)
        try:
            requests={r[0] for r in db.execute('SELECT key FROM requests')}
            responses={r[0] for r in db.execute('SELECT id FROM responses')}
            models=set(db.execute('SELECT response_id,model FROM models'))
            for parent in map(Path,backups):
                for path in parent.glob('recovered-model-ledger.json'):
                    ledger=json.loads(path.read_text())
                    missing=len(set(ledger['requests'])-requests)+len(set(ledger['responses'])-responses)
                    missing+=len({(rid,model) for rid,row in ledger['responses'].items() for model in row['models']}-models)
                    metadata_checks.append({'path':str(path),'missing_records':missing});metadata_missing+=missing
                for path in parent.glob('records-before.sqlite3'):
                    old=sqlite3.connect('file:'+str(path)+'?mode=ro&immutable=1',uri=True)
                    try:
                        missing=len({r[0] for r in old.execute('SELECT key FROM requests')}-requests)
                        missing+=len({r[0] for r in old.execute('SELECT id FROM responses')}-responses)
                    finally:old.close()
                    metadata_checks.append({'path':str(path),'missing_records':missing});metadata_missing+=missing
        finally:db.close()
    elif any(list(Path(p).glob('records-before.sqlite3')) or list(Path(p).glob('recovered-model-ledger.json')) for p in backups):
        raise ValueError('No main model index is available to verify backup metadata.')
    safe=not (missing_ws or body_failures or missing_flows or warnings or metadata_missing)
    result={'source':str(source),'source_files':source_files,'candidates':candidates,
            'safe_to_remove_recovery_copies':safe,'verified_websocket_payloads':original_ws-missing_ws,
            'verified_http_byte_ranges':body_verified,'missing_websocket_payloads':missing_ws,
            'missing_or_different_http_ranges':body_failures,'missing_flows':missing_flows,'warnings':warnings,
            'metadata_checks':metadata_checks,'missing_metadata_records':metadata_missing}
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({k:v for k,v in result.items() if k not in ('source_files','candidates','missing_or_different_http_ranges','missing_flows')},indent=2))
    print('Recovery-copy bytes: '+str(sum(c['bytes'] for c in candidates)))
    print('Unverified HTTP ranges: '+str(len(body_failures))+'; unverified flows: '+str(len(missing_flows)))
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',required=True);parser.add_argument('--backup',action='append',required=True);parser.add_argument('--output',required=True)
    args=parser.parse_args();audit(args.source,args.backup,args.output)
