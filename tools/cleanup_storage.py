#!/usr/bin/env python3
"""Filter historical traffic and remove verified redundant recovery/cache copies.

Requires the managed proxy to be stopped. Never removes model indexes, journals,
settings, certificates, source backups or data not covered by a validated plan.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import controller
from retention import POLICY
from traffic_archive import archive_files, iter_records
from compact_captures import prepare as compact, digest
from audit_cleanup import audit, manifest


def proxy_is_running(source):
    controller.DATA = Path(source)
    health = source/'archive-health.json'
    value = json.loads(health.read_text()) if health.exists() else {}
    return controller.rpc('status').get('running', False) or value.get('ready', False)


def db_info(source):
    path=source/'records.sqlite3'
    if not path.exists():return {}
    db=sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True)
    try:
        if db.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ValueError('Model database integrity check failed.')
        return {name:db.execute('SELECT COUNT(*) FROM '+name).fetchone()[0] for name in ('requests','responses','models','response_flows')}
    finally:db.close()


def protected_files(source):
    paths=[p for p in source.glob('*') if p.is_file() and p.name not in ('start.lock','control.sock')]
    for name in ('journals','CertificateAuthority'):
        paths.extend(p for p in (source/name).rglob('*') if p.is_file())
    # SQLite sidecar metadata may change when another reader opens the database.
    return {str(p.relative_to(source)):digest(p) for p in paths if not p.name.endswith(('-shm','-wal'))}


def assert_tree(path, expected):
    if manifest(path)!=expected:raise ValueError('Files changed after validation: '+str(path))


def prepare(source, project, work):
    source=Path(source).resolve();project=Path(project).resolve();work=Path(work).resolve()
    if proxy_is_running(source):raise ValueError('Stop the proxy in Model Monitor before cleaning stored data.')
    work.mkdir(parents=True,exist_ok=True,mode=0o700)
    filtered=work/'filtered'
    report_path=filtered/'compaction-report.json'
    report=json.loads(report_path.read_text()) if report_path.exists() else compact(source,filtered)
    if report['source']!=str(source) or report['excluded_active_session'] is not None:
        raise ValueError('This plan does not cover the stopped proxy\'s complete archive.')
    candidates=sorted((source/'Recovery').glob('restore-*'))+sorted((project/'backups').glob('capture-recovery-*'))
    candidates=[p for p in candidates if p.is_dir()]
    audit_path=work/'recovery-audit.json'
    evidence=json.loads(audit_path.read_text()) if audit_path.exists() else audit(source,candidates,audit_path)
    if not evidence['safe_to_remove_recovery_copies']:raise ValueError('A recovery copy contains data not proven to exist in the main archive. Originals were preserved.')
    expected_source=[{'relative':f['relative'],'bytes':f['bytes'],'sha256':f['sha256']} for f in report['source_files']]
    if sorted(expected_source,key=lambda r:r['relative'])!=sorted(evidence['source_files'],key=lambda r:r['relative']):
        raise ValueError('Archive and recovery validation used different source versions.')
    if {str(p.resolve()) for p in candidates}!={c['path'] for c in evidence['candidates']}:
        raise ValueError('Recovery directories changed after validation.')
    remove=[{'path':c['path'],'files':c['files'],'bytes':c['bytes'],'reason':'Verified duplicate recovery data'} for c in evidence['candidates']]
    for p in sorted((source/'Exports').glob('*/packet-*')):
        if p.is_dir():
            files=manifest(p);remove.append({'path':str(p.resolve()),'files':files,'bytes':sum(f['bytes'] for f in files),'reason':'Regenerable packet preview cache'})
    plan={'source':str(source),'project':str(project),'filtered':str(filtered),'policy':POLICY,
          'source_files':expected_source,'output_files':manifest(filtered/'Captures'),'remove':remove,
          'model_counts':db_info(source),'protected_files':protected_files(source),
          'source_bytes':report['source_bytes'],'filtered_bytes':report['filtered_bytes'],
          'protected_payloads_verified':report['protected_payloads_verified'],
          'recovery_websocket_payloads_verified':evidence['verified_websocket_payloads'],
          'recovery_http_ranges_verified':evidence['verified_http_byte_ranges'],
          'expected_saved_bytes':report['saved_bytes']+sum(item['bytes'] for item in remove)}
    path=work/'cleanup-plan.json';path.write_text(json.dumps(plan,indent=2)+'\n')
    return path


def apply(plan_path):
    os.umask(0o077)
    plan_path=Path(plan_path).resolve();plan=json.loads(plan_path.read_text())
    source=Path(plan['source']).resolve();project=Path(plan['project']).resolve();filtered=Path(plan['filtered']).resolve()
    if plan['policy']!=POLICY:raise ValueError('The prepared retention policy differs from this version.')
    for item in plan['remove']:
        path=Path(item['path']).resolve()
        allowed=(path.parent==source/'Recovery' and path.name.startswith('restore-')) or (
            path.parent==project/'backups' and path.name.startswith('capture-recovery-')) or (
            path.parent.parent==source/'Exports' and path.name.startswith('packet-'))
        if not allowed:raise ValueError('Unapproved cleanup location: '+str(path))
    with (source/'start.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if proxy_is_running(source):raise ValueError('The proxy restarted. Stop it and prepare a fresh cleanup plan.')
        expected=[dict(f,relative=str(Path(f['relative']).relative_to('Captures'))) for f in plan['source_files']]
        assert_tree(source/'Captures',sorted(expected,key=lambda r:r['relative']))
        assert_tree(filtered/'Captures',plan['output_files'])
        for item in plan['remove']:assert_tree(Path(item['path']),item['files'])
        if db_info(source)!=plan['model_counts'] or protected_files(source)!=plan['protected_files']:
            raise ValueError('Metadata changed after planning. Prepare a fresh plan; no data was deleted.')
        receipt=source/'Cleanup'/('cleanup-'+str(time.time_ns()));receipt.mkdir(parents=True,mode=0o700)
        # Keep the small proof and an operation journal, not another raw backup.
        shutil.copy2(plan_path,receipt/'plan.json')
        transaction={'phase':'staging','originals':str(receipt/'original-Captures'),'removed':[]}
        def save():
            temporary=receipt/'transaction.tmp'
            with temporary.open('w') as stream:
                json.dump(transaction,stream,indent=2);stream.flush();os.fsync(stream.fileno())
            temporary.replace(receipt/'transaction.json')
        save()
        staged=receipt/'new-Captures';original=receipt/'original-Captures';installed=source/'Captures'
        shutil.copytree(filtered/'Captures',staged)
        assert_tree(staged,plan['output_files'])
        warnings=[]
        for path in staged.glob('*/*'):
            if path.is_file():
                for _ in iter_records(path,warnings):pass
                with path.open('rb') as stream:os.fsync(stream.fileno())
        if warnings:raise ValueError('Staged archive contains an incomplete record; original files remain untouched.')
        if proxy_is_running(source):raise ValueError('The proxy restarted during staging. Nothing was replaced.')
        switched=False;committed=False
        try:
            installed.rename(original)
            transaction['phase']='originals-moved';save()
            staged.rename(installed);switched=True
            transaction['phase']='installed';save()
            assert_tree(installed,plan['output_files'])
            if db_info(source)!=plan['model_counts'] or protected_files(source)!=plan['protected_files']:
                raise ValueError('Metadata changed during replacement. Original archives will be restored.')
            transaction['phase']='verified';save();committed=True
            # Only now discard excluded originals. Every retained payload and
            # every recovery copy has passed its independent checksum checks.
            shutil.rmtree(original)
            for item in plan['remove']:
                path=Path(item['path']);assert_tree(path,item['files']);shutil.rmtree(path)
                transaction['removed'].append({'path':str(path),'bytes':item['bytes'],'reason':item['reason']});save()
            transaction.update(phase='complete',source_bytes=plan['source_bytes'],filtered_bytes=plan['filtered_bytes'],
                               saved_bytes=plan['source_bytes']-plan['filtered_bytes']+sum(v['bytes'] for v in transaction['removed']),
                               model_counts=db_info(source),policy=POLICY)
            save()
        except BaseException:
            if not committed and original.exists():
                if switched and installed.exists():installed.rename(receipt/'failed-new-Captures')
                original.rename(installed);transaction['phase']='rolled-back';save()
            raise
        # The installed copy was verified after replacement; discard our
        # temporary output so cleanup actually releases disk space.
        shutil.rmtree(filtered/'Captures')
        print(json.dumps({'receipt':str(receipt),'saved_bytes':transaction['saved_bytes'],'model_counts':transaction['model_counts'],'phase':transaction['phase']},indent=2))
        return receipt


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source');parser.add_argument('--project');parser.add_argument('--work')
    parser.add_argument('--prepared',help='Apply an already validated cleanup-plan.json')
    parser.add_argument('--prepare-only',action='store_true')
    args=parser.parse_args()
    try:
        if args.prepared:plan=Path(args.prepared)
        else:
            project=Path(args.project).resolve() if args.project else Path(__file__).resolve().parents[1]
            source=Path(args.source).resolve() if args.source else controller.DATA
            work=Path(args.work).resolve() if args.work else project/'build'/('cleanup-'+str(time.time_ns()))
            print('Validating and filtering existing traffic. The originals remain in place until validation succeeds.',flush=True)
            plan=prepare(source,project,work)
        if args.prepare_only:print(plan)
        else:apply(plan)
    except Exception as error:
        print('Cleanup needs attention: '+str(error),file=sys.stderr);sys.exit(1)
