#!/usr/bin/env python3
"""Private local controller for the native Model Monitor app. Python stdlib only."""
import argparse
import collections
import csv
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import socket
import socketserver
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
import sqlite3

from store import Store
import model_report
import traffic_archive
from capture_diagnostics import current_issues
from packet_details import packet_detail

ROOT = Path(sys.executable).resolve().parent.parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent
DEFAULTS = json.loads((ROOT/'defaults.json').read_text()) if (ROOT/'defaults.json').exists() else {}
DEFAULT_DATA = Path(os.environ.get('MODELSCOPE_DEFAULT_DATA_DIR', str(Path.home() / 'Library/Application Support/Model Monitor'))).expanduser().resolve()
LEGACY_DATA = Path(os.environ.get('MODELSCOPE_LEGACY_DATA_DIR', str(Path.home() / 'Library/Application Support/ModelScope'))).expanduser()
PREVIOUS_DATA = Path.home() / 'Library/Application Support/GPT Model Trace'
LOCATION_FILE = Path(os.environ.get('MODELSCOPE_LOCATION_FILE', str(Path.home() / 'Library/Preferences/local.yijia.modeltrace-location.json'))).expanduser()


def data_location():
    if os.environ.get('MODELSCOPE_DATA_DIR'):
        return Path(os.environ['MODELSCOPE_DATA_DIR']).expanduser().resolve()
    if LOCATION_FILE.is_file():
        return Path(json.loads(LOCATION_FILE.read_text())['data_dir']).expanduser().resolve()
    # Preserve the existing installation's records until the user moves them.
    for previous in (LEGACY_DATA, PREVIOUS_DATA):
        if (previous / 'settings.json').exists() or (previous / 'records.sqlite3').exists():
            return previous.resolve()
    if DEFAULTS.get('data_dir'):
        return Path(DEFAULTS['data_dir']).expanduser().resolve()
    return DEFAULT_DATA


DATA = data_location()
os.umask(0o077)


def bootstrap_data():
    """On the user's first launch, move the preview profile without interrupting its proxy."""
    if 'MODELSCOPE_DATA_DIR' in os.environ or DEFAULTS.get('data_dir'):
        return
    legacy = Path(DEFAULTS.get('legacy_data_dir') or '/nonexistent')
    if DATA.exists() or not legacy.is_dir() or legacy.is_symlink():
        return
    DATA.parent.mkdir(parents=True, exist_ok=True)
    # Same-volume rename preserves open journals and the live control socket.
    # The compatibility link keeps an already-running worker's paths valid.
    legacy.rename(DATA)
    try:
        legacy.symlink_to(DATA, target_is_directory=True)
    except OSError:
        DATA.rename(legacy)
        raise


def config():
    bootstrap_data()
    if LOCATION_FILE.is_file() and not DATA.is_dir():
        raise ValueError('The selected data folder is missing. Restore it at: ' + str(DATA))
    DATA.mkdir(parents=True, exist_ok=True)
    p = DATA / 'settings.json'
    if p.exists():
        value = json.loads(p.read_text())
        # Preserve valid custom installations; recover moved bundled app paths.
        if not Path(value.get('mitmweb', '')).is_file():
            replacement = find_proxy()
            if replacement:
                value['mitmweb'] = replacement
                model_report.atomic_json(p, value)
        return value
    defaults = json.loads((ROOT / 'defaults.json').read_text()) if (ROOT / 'defaults.json').exists() else {}
    app = next((p for p in ['/Applications/ChatGPT.app','/Applications/Codex.app'] if Path(p).exists()), '/Applications/ChatGPT.app')
    proxy = find_proxy()
    old_ca = Path.home()/'.mitmproxy/mitmproxy-ca-cert.pem'
    ca = old_ca if old_ca.is_file() and (old_ca.parent/'mitmproxy-ca.pem').is_file() else DATA/'CertificateAuthority/mitmproxy-ca-cert.pem'
    value = {'gpt_app':app, 'mitmweb':proxy, 'ca_file':str(ca),
             'proxy_port':18741,'web_port':18742, 'web_token':secrets.token_urlsafe(32), 'legacy_ledger':defaults.get('legacy_ledger','')}
    model_report.atomic_json(p,value)
    return value


def find_proxy():
    candidates = [ROOT/'mitmproxy.app/Contents/MacOS/mitmweb', Path('/opt/homebrew/bin/mitmweb'), Path('/usr/local/bin/mitmweb')]
    return next((str(p) for p in candidates if p.is_file()), shutil.which('mitmweb') or '')


def prepare_setup(settings):
    """Create a local CA without changing Keychain trust or starting a listener."""
    if rpc('status').get('running'):
        raise ValueError('Stop the managed proxy before preparing capture setup.')
    proxy = Path(settings['mitmweb'])
    if not proxy.is_file():
        raise ValueError('The proxy runtime is missing. Install the complete Model Monitor package or choose mitmweb in Settings.')
    ca = Path(settings['ca_file']).expanduser()
    key = ca.parent/'mitmproxy-ca.pem'
    if ca.exists() != key.exists():
        raise ValueError('The certificate folder contains an incomplete CA. Choose the original matching certificate and key, or a new empty folder. Existing files were not replaced.')
    if not ca.exists():
        if ca.name != 'mitmproxy-ca-cert.pem':
            raise ValueError('Use mitmproxy-ca-cert.pem as the certificate filename for automatic setup.')
        dump = proxy.parent/'mitmdump'
        if not dump.is_file():
            raise ValueError('mitmdump is missing beside mitmweb. Install the complete proxy runtime.')
        ca.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        env = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT='1')
        for name in ('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy'):
            env.pop(name, None)
        result = subprocess.run([str(dump), '-q', '--set', 'server=false', '--set', 'confdir='+str(ca.parent),
                                 '-s', str(ROOT/'init_certificate.py')], capture_output=True, env=env, timeout=40)
        if result.returncode or not ca.is_file() or not key.is_file():
            raise ValueError('Could not create the local proxy certificate. ' + (result.stderr or result.stdout).decode('utf-8','replace')[-1200:])
        os.chmod(key, 0o600)
    return {'certificate_path':str(ca), 'certificate_trusted':ca_is_trusted(ca)}


def ca_is_trusted(ca):
    if not Path(ca).is_file():
        return False
    try:
        verify_ca(ca)
        return True
    except (CertificateTrustError, OSError, subprocess.TimeoutExpired):
        return False


def worker_command(ports):
    prefix = [sys.executable] if getattr(sys, 'frozen', False) else [sys.executable, str(ROOT/'controller.py')]
    return prefix + ['worker', '--ports', str(ports[0]), str(ports[1])]


def save_settings(value, settings):
    if rpc('status').get('running'):
        raise ValueError('Stop this proxy before changing settings or moving data.')
    for key in ('gpt_app', 'mitmweb', 'ca_file'):
        if not isinstance(value.get(key), str) or not Path(value[key]).exists():
            raise ValueError('Path not found: ' + key)
        settings[key] = value[key]
    for key in ('proxy_port', 'web_port'):
        port = int(value[key])
        if not 1024 <= port <= 65535:
            raise ValueError('Ports must be between 1024 and 65535.')
        settings[key] = port
    if settings['proxy_port'] == settings['web_port']:
        raise ValueError('The two ports must be different.')
    name = value.get('data_dir')
    if not isinstance(name, str) or not name.strip():
        raise ValueError('Choose a data folder.')
    path = Path(name).expanduser()
    if not path.is_absolute():
        raise ValueError('Choose an absolute data folder path.')
    target = path.resolve()
    if any(part.endswith('.app') for part in target.parts):
        raise ValueError('Choose a folder outside the application bundle.')
    if target == DATA:
        model_report.atomic_json(DATA / 'settings.json', settings)
        return
    if DATA in target.parents:
        raise ValueError('Choose a folder outside the current data folder.')
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise ValueError('The new data folder must be empty. Existing records will be copied into it.')
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.' + target.name + '-moving-', dir=target.parent))
    try:
        shutil.copytree(DATA, stage, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns('control.sock', 'records.sqlite3-wal', 'records.sqlite3-shm'))
        source_db = DATA / 'records.sqlite3'
        if source_db.exists():
            copied_db = stage / 'records.sqlite3'
            copied_db.unlink(missing_ok=True)
            source = sqlite3.connect(source_db)
            destination = sqlite3.connect(copied_db)
            try:
                source.backup(destination)
            finally:
                destination.close()
                source.close()
        try:
            relative_ca = Path(settings['ca_file']).resolve().relative_to(DATA)
        except ValueError:
            pass  # An explicitly selected external CA stays at its own location.
        else:
            settings['ca_file'] = str(target/relative_ca)
        model_report.atomic_json(stage / 'settings.json', settings)
        if target.exists():
            target.rmdir()
        stage.rename(target)
        try:
            LOCATION_FILE.parent.mkdir(parents=True, exist_ok=True)
            model_report.atomic_json(LOCATION_FILE, {'data_dir': str(target)})
        except Exception:
            # The old location remains intact if selecting the new copy fails.
            shutil.rmtree(target)
            raise
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def migrate(settings, store):
    if store.info('migration_complete'):
        return
    path = Path(settings.get('legacy_ledger') or '/nonexistent')
    if path.is_file():
        store.merge(model_report.read_ledger(path))
    store.info('migration_complete', 'yes')


def sync_journals(store):
    for path in sorted((DATA/'journals').glob('*.jsonl')):
        key='journal:'+path.name
        offset=int(store.info(key) or 0)
        with path.open('rb') as stream:
            stream.seek(offset)
            while True:
                line=stream.readline()
                if not line or not line.endswith(b'\n'):
                    break  # writer may still be appending this record
                row=json.loads(line)
                store.event(row['event'],row['time'],row['flow_id'],row['from_client'],row['key'])
                # Commit each event before advancing its cursor. Replay is idempotent.
                store.info(key,stream.tell())


def capture_health():
    path=DATA/'capture-health.json'
    return json.loads(path.read_text()) if path.exists() else {}


def control_socket():
    path = DATA/'control.sock'
    if len(os.fsencode(path)) < 104:
        return path
    # macOS sockaddr_un is bounded. Keep long data-folder paths supported
    # without exposing the control socket to another local account.
    directory = Path('/private/tmp')/('model-monitor-'+str(os.getuid()))
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError('The private proxy control directory has unsafe permissions: '+str(directory))
    name = hashlib.sha256(os.fsencode(DATA)).hexdigest()[:32]
    return directory/(name+'.sock')


def rpc(command, timeout=1):
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(str(control_socket()))
            client.sendall(json.dumps({'command':command}).encode()+b'\n')
            with client.makefile('rb') as stream:
                line=stream.readline(65536)
            return json.loads(line)
    except (OSError, ValueError):
        return {'running':False}


def available(port):
    try:
        with socket.socket() as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(('127.0.0.1',port))
        return True
    except OSError:
        return False


CAPTURE_FILES = ('capture_addon.py', 'full_capture.py', 'retention.py', 'traffic_archive.py', 'capture_diagnostics.py')


def snapshot_capture_runtime(session):
    folder = DATA/'Runtime'/session
    folder.mkdir(parents=True, mode=0o700)
    for name in CAPTURE_FILES:
        shutil.copy2(ROOT/name, folder/name)
    return folder/'capture_addon.py'


def recorder_status(settings, proxy, expected_session=None):
    if not proxy.get('running'):
        return False, ''
    try:
        health = json.loads((DATA/'archive-health.json').read_text())
        if not health.get('ready'):
            return False, health.get('error') or 'The proxy is forwarding traffic, but its recording plugin is not active.'
        if expected_session is not None and health.get('session') != expected_session:
            return False, 'The recording plugin belongs to a different proxy session.'
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        request = urllib.request.Request('http://127.0.0.1:'+str(proxy['web_port'])+'/options',
                                        headers={'Authorization':'Bearer '+settings['web_token']})
        with opener.open(request, timeout=1) as response:
            scripts = json.load(response).get('scripts', {}).get('value', [])
        if not any(Path(path).name == 'capture_addon.py' for path in scripts):
            return False, 'The recording plugin was removed from the proxy. New traffic is not being saved.'
        return True, ''
    except (OSError, ValueError, KeyError):
        return False, 'Could not verify that the proxy is recording. Check proxy.log before continuing.'


def proxy_command(settings, proxy_port, web_port, script=None):
    ca=Path(settings['ca_file']).expanduser()
    if not ca.is_file():
        raise ValueError('Proxy certificate not found. Choose mitmproxy-ca-cert.pem in Settings.')
    if not (ca.parent/'mitmproxy-ca.pem').is_file():
        raise ValueError('mitmproxy-ca.pem is missing. Choose the original mitmproxy certificate folder.')
    if not Path(settings['mitmweb']).is_file():
        raise ValueError('mitmweb not found. Choose the installed executable in Settings.')
    return [settings['mitmweb'],'--mode','regular','--listen-host','127.0.0.1','--listen-port',str(proxy_port),
            '--web-host','127.0.0.1','--web-port',str(web_port),'--no-web-open-browser','-q',
            '--set','confdir='+str(ca.parent),'--set','ssl_insecure=false','--set','flow_detail=0',
            '--set','store_streamed_bodies=true',
            '-s',str(script or ROOT/'capture_addon.py')]


def start(settings):
    with (DATA/'start.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state=rpc('status')
        if state.get('running'):
            recording, error = recorder_status(settings, state)
            if not recording:
                raise ValueError(error+' Stop and start the proxy to restore recording.')
            return state
        # Never take over another proxy. Fall back to a free private pair.
        pairs=[(settings['proxy_port'],settings['web_port'])]+[(p,p+1) for p in range(18741,18841,2)]
        pair=next(((p,w) for p,w in pairs if p!=w and available(p) and available(w)),None)
        if pair is None:
            raise ValueError('No available port pair. Close unused proxies or change the ports in Settings.')
        proxy_command(settings,*pair)
        log=(DATA/'proxy.log').open('ab',buffering=0)
        env=dict(os.environ,MODELSCOPE_DATA_DIR=str(DATA),PYINSTALLER_RESET_ENVIRONMENT='1')
        process=subprocess.Popen(worker_command(pair),
                                 stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True,env=env)
        log.close()
        for _ in range(100):
            state=rpc('status',0.2)
            if state.get('running'):
                return state
            if process.poll() is not None:
                raise ValueError('Proxy startup failed. Open proxy.log in the data folder for details.')
            time.sleep(.15)
        raise ValueError('Proxy startup timed out. Refresh in a moment or check proxy.log.')


def worker(settings, ports):
    session=str(time.time_ns())+'-'+secrets.token_hex(4)
    script=snapshot_capture_runtime(session)
    command=proxy_command(settings,*ports,script=script)
    env=dict(os.environ,MODELSCOPE_DATA_DIR=str(DATA),MODELSCOPE_WEB_TOKEN=settings['web_token'],MODELSCOPE_CAPTURE_SESSION=session,PYINSTALLER_RESET_ENVIRONMENT='1')
    for key in ('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy'):
        env.pop(key,None)
    proc=subprocess.Popen(command,env=env,stdin=subprocess.DEVNULL)
    server=None
    shutdown_error=None
    stop=[False]
    def mark_stop(*_):stop[0]=True
    signal.signal(signal.SIGTERM,mark_stop)
    signal.signal(signal.SIGINT,mark_stop)
    try:
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        ready=False
        for _ in range(90):
            if proc.poll() is not None or stop[0]:break
            try:
                req=urllib.request.Request(f'http://127.0.0.1:{ports[1]}/state.json',headers={'Authorization':'Bearer '+settings['web_token']})
                with opener.open(req,timeout=.25) as response:
                    ready=response.status==200
                health_path=DATA/'archive-health.json'
                archive_health=json.loads(health_path.read_text()) if health_path.exists() else {}
                ready=ready and archive_health.get('ready') and archive_health.get('session')==session
                if ready:break
            except (OSError,ValueError):pass
            time.sleep(.15)
        if not ready:
            raise ValueError('mitmweb failed the startup check.')
        sock=control_socket()
        if sock.exists():sock.unlink()
        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                try:
                    self.connection.settimeout(1)
                    message=json.loads(self.rfile.readline(4096))
                    action=message.get('command')
                    result={'running':proc.poll() is None,'proxy_port':ports[0],'web_port':ports[1],'archive_version':1}
                    if action=='stop':stop[0]=True
                    if action not in ('stop','status'):result={'error':'unknown command'}
                    self.wfile.write(json.dumps(result).encode()+b'\n')
                except (OSError,ValueError):pass
        server=socketserver.UnixStreamServer(str(sock),Handler)
        os.chmod(sock,0o600)
        server.timeout=.5
        next_check = 0
        failed_checks = 0
        while proc.poll() is None and not stop[0]:
            server.handle_request()
            if not stop[0] and time.monotonic() >= next_check:
                next_check = time.monotonic() + 1
                recording, error = recorder_status(settings, {'running':True, 'web_port':ports[1]}, session)
                failed_checks = 0 if recording else failed_checks + 1
                if failed_checks >= 2:
                    shutdown_error=error+' The managed proxy has been stopped to prevent further unsaved traffic.'
                    break
    finally:
        if server:
            server.server_close()
            sock.unlink(missing_ok=True)
        if proc.poll() is None:
            proc.terminate()
            try:proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill();proc.wait()
        if shutdown_error:
            model_report.atomic_json(DATA/'archive-health.json',
                {'archive_version':1, 'ready':False, 'session':session, 'error':shutdown_error})


class CertificateTrustError(ValueError):
    pass


def verify_ca(ca):
    # -l explicitly allows a CA as the evaluated leaf. Without it even a
    # trusted local root can return failure. -L keeps this check local.
    check=subprocess.run(['/usr/bin/security','verify-cert','-c',str(ca),'-p','ssl','-l','-L','-q'],capture_output=True,timeout=10)
    if check.returncode:
        raise CertificateTrustError('The selected proxy CA did not pass the macOS trust check. Check the certificate selected in Settings and its trust in Keychain Access, then retry.')


def launch_plan(settings):
    state=rpc('status')
    if not state.get('running'):
        raise ValueError('Start the proxy before launching GPT.')
    app=Path(settings['gpt_app']).expanduser()
    if not (app/'Contents/Info.plist').is_file():
        raise ValueError('GPT application not found. Choose it in Settings.')
    ca=Path(settings['ca_file']).expanduser()
    verify_ca(ca)
    # Preserve system roots for all other TLS destinations.
    bundle=DATA/'trusted-ca-bundle.pem'
    roots=Path('/etc/ssl/cert.pem').read_bytes() if Path('/etc/ssl/cert.pem').is_file() else b''
    bundle.write_bytes(roots+b'\n'+ca.read_bytes())
    proxy=f'http://127.0.0.1:{state["proxy_port"]}'
    environment={k:proxy for k in ('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy')}
    environment.update(NO_PROXY='localhost,127.0.0.1,::1',no_proxy='localhost,127.0.0.1,::1',
                       NODE_EXTRA_CA_CERTS=str(ca),SSL_CERT_FILE=str(bundle),REQUESTS_CA_BUNDLE=str(bundle))
    return {'app':str(app),'environment':environment,'arguments':['--proxy-server='+proxy]}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('command',choices=['status','start','stop','worker','launch-plan','import','export','settings','web-url','export-traffic','packet-detail','prepare-setup'])
    parser.add_argument('--ports',nargs=2,type=int)
    parser.add_argument('--date')
    parser.add_argument('--path')
    parser.add_argument('--mode',choices=['requests','responses'])
    parser.add_argument('--id')
    args=parser.parse_args()
    settings=config()
    if args.command=='prepare-setup':
        print(json.dumps({'ok':True, **prepare_setup(settings)}))
        return
    if args.command=='worker':
        worker(settings,args.ports);return
    if args.command=='settings':
        save_settings(json.load(sys.stdin), settings)
        print(json.dumps({'ok':True},ensure_ascii=False))
        return
    store=Store(DATA)
    try:
        migrate(settings,store)
        sync_journals(store)
        if args.command=='status':
            public={k:v for k,v in settings.items() if k not in ('web_token','legacy_ledger')}
            public['data_dir'] = str(DATA)
            health=capture_health()
            proxy=rpc('status')
            recording, recording_error = recorder_status(settings, proxy)
            proxy['recording'] = recording
            archive_health_path=DATA/'archive-health.json'
            archive_health=json.loads(archive_health_path.read_text()) if archive_health_path.exists() else {}
            report=store.report(args.date)
            policy = archive_health.get('storage_policy', {})
            archive_note=('Astra and codex-auto-review: summaries. Other and unclassified traffic: full content.'
                          if policy.get('version') == 1 else 'This worker saves full traffic. Restart the proxy to apply model retention rules.')
            if proxy.get('running') and not proxy.get('archive_version'):
                archive_note='This proxy was started by an older version. Stop and start the proxy to enable full traffic recording.'
            issues = current_issues(health,proxy.get('running',False))
            if proxy.get('running') and not recording:
                issues.insert(0, {'id':'recorder:'+str(archive_health.get('session','unknown')),
                    'title':'Traffic recording has stopped',
                    'message':recording_error+' Stop and start the proxy to restore recording. Existing archives are unchanged.'})
            result={'ok':True,'proxy':proxy,'settings':public,'data_dir':str(DATA),
                    'default_data_dir':str(DEFAULT_DATA),
                    'report':report,'capture_error':recording_error or archive_health.get('error') or health.get('capture_error',''),
                    'archive':traffic_archive.summary(DATA,report['date']),'archive_note':archive_note,
                    'capture_issues':issues,
                    'dependencies':{'mitmweb':Path(settings['mitmweb']).is_file(),
                                    'bundled_proxy':(ROOT/'mitmproxy.app/Contents/MacOS/mitmweb').is_file(),
                                    'certificate':Path(settings['ca_file']).is_file() and (Path(settings['ca_file']).parent/'mitmproxy-ca.pem').is_file(),
                                    'certificate_trusted':ca_is_trusted(settings['ca_file'])}}
        elif args.command=='start':
            result={'ok':True,'proxy':start(settings)}
        elif args.command=='stop':
            rpc('stop')
            for _ in range(100):
                if not rpc('status',.1).get('running'):break
                time.sleep(.1)
            if rpc('status').get('running'):raise ValueError('The proxy is still stopping. Refresh in a moment.')
            result={'ok':True}
        elif args.command=='launch-plan':
            result={'ok':True,**launch_plan(settings)}
        elif args.command=='web-url':
            state=rpc('status')
            if not state.get('running'):raise ValueError('Start the proxy first.')
            result={'ok':True,'url':f'http://127.0.0.1:{state["web_port"]}/?token={settings["web_token"]}'}
        elif args.command=='import':
            if not args.path:raise ValueError('Choose a flows file.')
            with Path(args.path).open('rb') as stream:
                ledger=model_report.empty_ledger()
                stats=model_report.ingest(ledger,model_report.iter_flows(stream))
            store.merge(ledger)
            result={'ok':True,'requests':len(ledger['requests']),'responses':len(ledger['responses'])}
        elif args.command=='export-traffic':
            result={'ok':True,**traffic_archive.export_day(DATA,args.date or traffic_archive.date_key())}
        elif args.command=='packet-detail':
            if not args.mode or not args.id:
                raise ValueError('Choose a model record to view.')
            result={'ok':True,**packet_detail(DATA,store,args.mode,args.id)}
        elif args.command=='export':
            report=store.report(args.date)
            folder=DATA/'Exports'/report['date']
            folder.mkdir(parents=True,exist_ok=True)
            for name in ('requests','responses'):
                with (folder/(name+'.csv')).open('w',encoding='utf-8-sig',newline='') as stream:
                    writer=csv.writer(stream);writer.writerow(['Time (Hong Kong)','Model','Status','ID'])
                    for r in report[name]:
                        cells=[r['time'],r['model'],r['status'],r['id']]
                        writer.writerow(["'"+v if v.startswith(('=','+','-','@','\t','\r')) else v for v in cells])
            model_report.atomic_json(folder/'summary.json',{k:v for k,v in report.items() if k not in ('requests','responses')})
            result={'ok':True,'path':str(folder)}
        print(json.dumps(result,ensure_ascii=False))
    finally:store.close()


if __name__=='__main__':
    try:main()
    except Exception as exc:
        print(json.dumps({'ok':False,'error':str(exc),'code':'certificate_trust' if isinstance(exc,CertificateTrustError) else 'helper_error'},ensure_ascii=False))
        sys.exit(1)
