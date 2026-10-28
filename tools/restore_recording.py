#!/usr/bin/env python3
"""User-run repair: preserve retained flows, restart capture, recover saved records."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import time
import urllib.request

PROJECT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT/'src'))
import controller
import model_report
from store import Store
from traffic_archive import archive_files,iter_records
from recover_archive import recover_archive


def main():
    os.umask(0o077)
    settings=controller.config();root=controller.DATA
    state=controller.rpc('status')
    backup=root/'Recovery'/('restore-'+str(time.time_ns()))
    backup.mkdir(parents=True,mode=0o700)
    if state.get('running'):
        print('Saving all flows still retained by the proxy...',flush=True)
        file=backup/'retained-proxy-flows.mitm';temporary=file.with_suffix('.partial')
        request=urllib.request.Request('http://127.0.0.1:'+str(state['web_port'])+'/flows/dump',
                                        headers={'Authorization':'Bearer '+settings['web_token']})
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        digest=hashlib.sha256()
        with opener.open(request,timeout=60) as response,temporary.open('xb') as output:
            while True:
                chunk=response.read(1024*1024)
                if not chunk:break
                output.write(chunk);digest.update(chunk)
            output.flush();os.fsync(output.fileno())
        temporary.rename(file)
        meta={'path':str(file),'sha256':digest.hexdigest(),'captured_at':time.time()}
        model_report.atomic_json(backup/'snapshot.json',meta)
    else:
        saved=sorted((PROJECT/'backups').glob('capture-recovery-*/snapshot.json'))
        if not saved:raise RuntimeError('No running proxy or retained-flow backup was found.')
        meta=json.loads(saved[-1].read_text())
        if not Path(meta['path']).is_file():raise RuntimeError('The retained-flow backup is missing.')
    # Complete validation before stopping the existing proxy.
    ledger=model_report.empty_ledger()
    with Path(meta['path']).open('rb') as stream:stats=model_report.ingest(ledger,model_report.iter_flows(stream))
    print('Validated '+str(stats.get('flows',0))+' retained flows. Restarting the proxy...',flush=True)
    if state.get('running'):
        controller.rpc('stop')
        for _ in range(150):
            if not controller.rpc('status').get('running') and all(controller.available(state[k]) for k in ('proxy_port','web_port')):break
            time.sleep(.1)
        else:raise RuntimeError('The old proxy has not released its ports. Retry after it finishes stopping.')
        settings=dict(settings,proxy_port=state['proxy_port'],web_port=state['web_port'])
    fresh=controller.start(settings)
    if state.get('running') and fresh['proxy_port']!=state['proxy_port']:
        raise RuntimeError('The old proxy port became occupied. Relaunch the target app using Model Monitor.')
    print('Recording is running. Recovering retained request/response packets...',flush=True)
    report=recover_archive(meta,root,backup/'archive')
    for path in archive_files(backup/'archive'):
        # Check every checksum before making this file visible to the app.
        for _ in iter_records(path):pass
        target=root/'Captures'/path.parent.name/path.name
        target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        if target.exists():raise RuntimeError('Refusing to overwrite an existing archive: '+str(target))
        temporary=target.with_suffix('.recovering')
        shutil.copy2(path,temporary);temporary.replace(target)
    database=root/'records.sqlite3'
    if database.exists():
        original=sqlite3.connect('file:'+str(database)+'?mode=ro',uri=True)
        saved=sqlite3.connect(backup/'records-before.sqlite3')
        try:original.backup(saved)
        finally:saved.close();original.close()
    store=Store(root)
    try:
        before=store.db.execute('SELECT COUNT(*) FROM responses').fetchone()[0]
        store.merge(ledger)
        added=store.db.execute('SELECT COUNT(*) FROM responses').fetchone()[0]-before
    finally:store.close()
    print('Recovered '+str(added)+' response model records. Original snapshot and database backup: '+str(backup),flush=True)
    if report['warnings']:
        print(str(len(report['warnings']))+' body prefixes could not be merged. Complete retained snapshots remain in the backup.')
    if not controller.recorder_status(settings,controller.rpc('status'))[0]:
        raise RuntimeError('The recorder needs attention. Open Model Monitor and check its recording status.')
    print('Done. Model Monitor will refresh automatically. Active connections may need to reconnect after the proxy restart.')


if __name__=='__main__':
    try:main()
    except Exception as error:
        print('Repair did not finish: '+str(error),file=sys.stderr)
        sys.exit(1)
