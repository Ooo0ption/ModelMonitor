"""Recover retained flow bodies/messages, skipping already archived bytes/events."""
from pathlib import Path
import collections
import hashlib
import json
import os
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import model_report
from traffic_archive import ArchiveWriter, archive_files, iter_records


def recover_archive(meta, source, destination):
    os.umask(0o077)
    snapshot=Path(meta['path'])
    source=Path(source);destination=Path(destination)
    destination.mkdir(parents=True,mode=0o700)
    body_index={}
    events=set()
    messages=set()
    for path in archive_files(source):
        for event,payload in iter_records(path):
            fid=event.get('flow_id');kind=event['kind']
            if kind=='websocket.message':
                messages.add((fid,event.get('message_index'),event.get('time')))
            elif fid and kind in ('http.request.body','http.response.body'):
                key=(fid,kind);item=body_index.setdefault(key,{'size':0,'digest':hashlib.sha256(),'contiguous':True})
                if event.get('offset',0)!=item['size']:item['contiguous']=False
                item['digest'].update(payload);item['size']+=len(payload)
            elif fid:events.add((fid,kind))
    writer=ArchiveWriter(destination,'recovered-'+str(time.time_ns()))
    writer.write({'kind':'session.start','time':time.time(),'recovered':True,'source_snapshot':snapshot.name,
                  'scope':'Retained application contents recovered from a native proxy snapshot. Original chunk timing is unavailable.'})
    counts=collections.Counter();warnings=[]
    with snapshot.open('rb') as stream:
        for flow in model_report.iter_flows(stream):
            fid=model_report.text(flow.get('id'))
            if model_report.text(flow.get('type'))!='http':continue
            state={key:flow.get(key) for key in ('id','type','timestamp_created','client_conn','server_conn','error','metadata','comment','is_replay')}
            state['id']=fid;state['type']='http'
            for side in ('request','response'):
                message=flow.get(side)
                if isinstance(message,dict):state[side]={k:v for k,v in message.items() if k!='content'}
            ws=flow.get('websocket')
            if isinstance(ws,dict):state['websocket']={k:v for k,v in ws.items() if k!='messages'}
            req=flow.get('request') or {}
            state['url']=model_report.text(req.get('scheme'))+'://'+model_report.text(req.get('host'))+model_report.text(req.get('path'))
            def emit(kind,stamp,payload=b'',**extra):
                writer.write(dict(kind=kind,flow_id=fid,time=stamp,recovered=True,**extra),payload,state)
                counts[kind]+=1
            for side in ('request','response'):
                message=flow.get(side)
                if not isinstance(message,dict):continue
                start=message.get('timestamp_start') or flow.get('timestamp_created') or meta['captured_at']
                if (fid,'http.'+side+'.headers') not in events:
                    emit('http.'+side+'.headers',start,state=state)
                payload=message.get('content')
                if isinstance(payload,bytes):
                    kind='http.'+side+'.body';old=body_index.get((fid,kind))
                    offset=old['size'] if old else 0
                    valid=(old is None or (old['contiguous'] and offset<=len(payload) and hashlib.sha256(payload[:offset]).digest()==old['digest'].digest()))
                    if not valid:
                        warnings.append({'flow_id':fid,'side':side,'reason':'Saved body is not an exact prefix; full body retained only in native snapshot.'})
                    elif len(payload)>offset:
                        emit(kind,message.get('timestamp_end') or meta['captured_at'],payload[offset:],offset=offset,original_timestamp_start=start)
                if message.get('timestamp_end') and (fid,'http.'+side+'.end') not in events:
                    emit('http.'+side+'.end',message['timestamp_end'],state=state)
            if isinstance(ws,dict):
                for index,message in enumerate(ws.get('messages',[])):
                    opcode,from_client,payload,stamp=message[:4]
                    if (fid,index,stamp) in messages:continue
                    emit('websocket.message',stamp,payload,direction='client_to_server' if from_client else 'server_to_client',
                         opcode=opcode,message_index=index,dropped=message[4] if len(message)>4 else False,
                         injected=message[5] if len(message)>5 else False)
    writer.write({'kind':'session.stop','time':time.time(),'recovered':True})
    writer.close()
    report={'counts':dict(counts),'warnings':warnings,'files':len(archive_files(destination)),
            'bytes':sum(p.stat().st_size for p in archive_files(destination))}
    (destination/'recovery-report.json').write_text(json.dumps(report,indent=2))
    return report
