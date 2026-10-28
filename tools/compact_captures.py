#!/usr/bin/env python3
"""Prepare and validate filtered copies of CLOSED archives; never replace inputs."""
import argparse
import collections
import hashlib
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from packet_details import MessageLinks
from retention import SelectiveArchiveWriter, Probe, POLICY, summary_model
from traffic_archive import archive_files,iter_records,_text


def digest(path):
    result=hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):result.update(chunk)
    return result.hexdigest()


def prepare(source, output):
    os.umask(0o077);source=Path(source).resolve();output=Path(output).resolve()
    if output==source or source/'Captures' in output.parents:raise ValueError('Choose a separate output folder.')
    if output.exists():raise ValueError('The output folder must not exist yet.')
    health=source/'archive-health.json'
    health_value=json.loads(health.read_text()) if health.exists() else {}
    active=health_value.get('archive_session') if health_value.get('ready', True) else None
    files=[p for p in archive_files(source) if not active or not p.name.startswith(active+'-')]
    if not files:raise ValueError('No closed archives are available yet.')
    output.mkdir(parents=True,mode=0o700)
    links=MessageLinks();response_models={};http_probes={};warnings=[];source_info=[]
    def reference(event):return (event['session'],event['sequence'])
    for path in files:
        info={'path':str(path),'relative':str(path.relative_to(source)),'bytes':path.stat().st_size,'sha256':digest(path)}
        source_info.append(info)
        for event,payload in iter_records(path,warnings):
            fid=event.get('flow_id');kind=event['kind']
            if kind=='websocket.message':
                try:value=json.loads(payload)
                except (ValueError,UnicodeDecodeError):continue
                if not isinstance(value,dict):continue
                key=f"{fid}:ws:{event['message_index']}:{event['time']}"
                links.add(fid,reference(event),key,event.get('direction')=='client_to_server',value)
                response=value.get('response')
                if isinstance(response,dict) and isinstance(response.get('id'),str) and isinstance(response.get('model'),str):
                    response_models[(fid,response['id'])]=response['model']
            elif kind=='http.response.headers':
                state=event.get('state') or {};message=state.get('response') or {}
                encoding=next((_text(v).lower() for k,v in message.get('headers',[]) if _text(k).lower()=='content-encoding'),'')
                http_probes.setdefault(fid,Probe(encoding))
            elif kind=='http.response.body' and fid in http_probes:http_probes[fid].feed(payload)
            elif kind=='http.response.end' and fid in http_probes:http_probes[fid].feed(b'',True)
    if warnings:raise ValueError('A closed archive has an incomplete record. Originals have not been changed.')
    links.finish();request_models={};ref_models={}
    for rid,item in links.responses.items():
        model=response_models.get((item['flow_id'],rid))
        if model:
            for ref in item['refs']:ref_models[ref]=model
    for key,item in links.requests.items():
        if len(item['responses'])==1:
            model=response_models.get((item['flow_id'],item['responses'][0]))
            if model:request_models[key]=model;ref_models[item['ref']]=model
    known={'requests':request_models,'responses':response_models,'items':links.items,
           'http':{fid:probe.model for fid,probe in http_probes.items() if probe.model}}
    writer=SelectiveArchiveWriter(output,'filtered-'+str(time.time_ns()),known=known,offline=True)
    expected={};contexts={};source_payload=collections.Counter();source_events=0
    try:
        for path in files:
            for event,payload in iter_records(path):
                source_events+=1;fid=event.get('flow_id');kind=event['kind'];ref=reference(event)
                if event.get('state'):contexts[fid]=event['state']
                if kind=='flow.context':continue
                origin=event.get('source_event') or (str(event['session'])+':'+str(event['sequence']))
                model=ref_models.get(ref)
                if kind.startswith('http.'):model=known['http'].get(fid)
                source_payload[model or 'unclassified']+=len(payload)
                # Every non-summary HTTP/transport body and every explicitly
                # identified other-model WS message must survive byte-for-byte.
                protect=(kind=='websocket.message' and model and not summary_model(model)) or (
                    kind in ('http.request.body','http.response.body','tcp.message','udp.message') and not summary_model(model))
                if protect:expected[origin]=(len(payload),hashlib.sha256(payload).hexdigest())
                writer.write(dict(event,source_event=origin),payload,contexts.get(fid))
    finally:writer.close()
    verified=set();output_events=0;summary_events=0;output_warnings=[]
    for path in archive_files(output):
        for event,payload in iter_records(path,output_warnings):
            output_events+=1;summary_events+=event.get('retention')=='summary'
            origin=event.get('source_event')
            if origin in expected:
                if (len(payload),hashlib.sha256(payload).hexdigest())!=expected[origin]:raise ValueError('Protected payload changed: '+origin)
                verified.add(origin)
    missing=set(expected)-verified
    if missing or output_warnings:raise ValueError('Filtered copy failed validation; originals have not been changed.')
    for info in source_info:
        if Path(info['path']).stat().st_size!=info['bytes'] or digest(Path(info['path']))!=info['sha256']:
            raise ValueError('A source archive changed during planning; originals have not been changed.')
    before=sum(item['bytes'] for item in source_info);after=sum(p.stat().st_size for p in archive_files(output))
    report={'policy':POLICY,'source':str(source),'output':str(output),'created_at':time.time(),
        'source_files':source_info,'source_bytes':before,'filtered_bytes':after,'saved_bytes':before-after,
        'source_events':source_events,'filtered_events':output_events,'summary_events':summary_events,
        'protected_payloads_verified':len(verified),'payload_bytes_by_model':dict(source_payload),
        'excluded_active_session':active,'warnings':output_warnings}
    (output/'compaction-report.json').write_text(json.dumps(report,indent=2))
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--source',required=True);parser.add_argument('--output',required=True)
    args=parser.parse_args();report=prepare(args.source,args.output)
    print(json.dumps({k:v for k,v in report.items() if k!='source_files'}))
