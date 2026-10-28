#!/usr/bin/env python3
"""Build a local, self-contained macOS installer. Does not install or trust a CA."""
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path, PurePosixPath
import platform
import plistlib
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def run(*args, **kwargs):
    subprocess.run([str(x) for x in args], check=True, **kwargs)


def download(url, destination):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=60) as response, destination.open('wb') as output:
        shutil.copyfileobj(response, output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mitmproxy-archive', type=Path, help='Use a cached official archive; its SHA-256 is still checked.')
    args = parser.parse_args()
    if sys.platform != 'darwin' or platform.machine() != 'arm64':
        raise SystemExit('Build this package on an Apple Silicon Mac.')
    importlib.metadata.version('pyinstaller')
    runtime = json.loads((ROOT/'packaging/runtime.json').read_text())
    (ROOT/'build').mkdir(exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='installer-', dir=ROOT/'build'))
    output = ROOT/'dist'; output.mkdir(exist_ok=True)
    payload = work/'payload'; app = payload/'Applications/Model Monitor.app'
    run('/bin/zsh', ROOT/'src/build.command', app)
    resources = app/'Contents/Resources'
    env = dict(os.environ, PYINSTALLER_CONFIG_DIR=str(work/'pyinstaller-cache'))
    run(sys.executable, '-m', 'PyInstaller', '--noconfirm', '--onedir', '--name', 'model-monitor-helper',
        '--target-architecture', 'arm64', '--distpath', work/'helper-dist', '--workpath', work/'helper-work',
        '--specpath', work, '--paths', ROOT/'src', ROOT/'src/controller.py', env=env)
    shutil.copytree(work/'helper-dist/model-monitor-helper', resources/'Helper')
    archive = args.mitmproxy_archive
    if archive is None:
        archive = work/'mitmproxy.tar.gz'
        download(runtime['url'], archive)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != runtime['sha256']:
        raise SystemExit('The mitmproxy archive checksum does not match the pinned official download.')
    unpack = work/'upstream'; unpack.mkdir()
    with tarfile.open(archive) as source:
        for member in source.getmembers():
            path = PurePosixPath(member.name)
            if path.is_absolute() or '..' in path.parts or path.parts[0] != 'mitmproxy.app':
                raise SystemExit('Unexpected path in the verified runtime archive: '+member.name)
        source.extractall(unpack)
    shutil.copytree(unpack/'mitmproxy.app', resources/'mitmproxy.app', symlinks=True)
    licenses = resources/'ThirdPartyLicenses'; licenses.mkdir()
    download(runtime['license_url'], licenses/'mitmproxy-LICENSE.txt')
    python_version = platform.python_version()
    download('https://raw.githubusercontent.com/python/cpython/v'+python_version+'/LICENSE', licenses/'Python-LICENSE.txt')
    distribution = importlib.metadata.distribution('pyinstaller')
    for item in distribution.files or []:
        if str(item).endswith('licenses/COPYING.txt'):
            shutil.copy2(distribution.locate_file(item), licenses/'PyInstaller-COPYING.txt')
            break
    else:
        raise SystemExit('Could not locate the PyInstaller runtime license.')
    shutil.copy2(ROOT/'LICENSE', resources/'LICENSE.txt')
    (resources/'THIRD_PARTY_NOTICES.md').write_text(
        '# Bundled runtimes\n\nmitmproxy '+runtime['version']+' is redistributed from its official, SHA-256-verified macOS archive. '
        'Its bundled dependency files and notices are retained. The helper embeds Python '+python_version+
        ' using PyInstaller '+distribution.version+'. See ThirdPartyLicenses for licenses.\n\n'
        'No user CA, private key, account credential, settings file or traffic capture is included. '
        'Certificates are created locally only when the user prepares capture setup.\n')
    shutil.copy2(ROOT/'packaging/runtime.json', resources/'bundled-runtime.json')
    run('codesign', '--force', '--deep', '--sign', '-', app)
    run('codesign', '--verify', '--deep', '--strict', app)
    with (app/'Contents/Info.plist').open('rb') as f: info = plistlib.load(f)
    components = work/'components.plist'
    run('pkgbuild', '--analyze', '--root', payload, components)
    with components.open('rb') as f: bundles = plistlib.load(f)
    for bundle in bundles:
        bundle['BundleIsRelocatable'] = False
    with components.open('wb') as f: plistlib.dump(bundles, f)
    component = work/'ModelMonitor.component.pkg'
    run('pkgbuild', '--root', payload, '--identifier', info['CFBundleIdentifier'],
        '--version', info['CFBundleShortVersionString'], '--install-location', '/',
        '--ownership', 'recommended', '--component-plist', components, component)
    ui = work/'installer-ui'; ui.mkdir()
    (ui/'welcome.html').write_text('<html><body><h1>Model Monitor</h1><p>Installs Model Monitor and its bundled proxy and Python runtimes in Applications. No Homebrew or separate runtime installation is needed.</p><p>After installation, open Model Monitor, prepare your local certificate, and approve SSL trust if you want HTTPS capture. The installer does not add trusted certificates, start a proxy, or change system proxy settings.</p><p>This is a local unsigned installer with an ad-hoc-signed app, not a notarized distribution. Requires Apple Silicon and macOS 14 or later.</p></body></html>')
    distribution_xml = work/'Distribution.xml'
    distribution_xml.write_text('<?xml version="1.0" encoding="utf-8"?>\n'
        '<installer-gui-script minSpecVersion="2"><title>Model Monitor</title>'
        '<welcome file="welcome.html" mime-type="text/html"/>'
        '<options customize="never" require-scripts="false" hostArchitectures="arm64"/>'
        '<allowed-os-versions><os-version min="14.0"/></allowed-os-versions>'
        '<domains enable_localSystem="true" enable_currentUserHome="false" enable_anywhere="false"/>'
        '<choices-outline><line choice="main"/></choices-outline>'
        '<choice id="main" visible="false" title="Model Monitor"><pkg-ref id="'+info['CFBundleIdentifier']+'"/></choice>'
        '<pkg-ref id="'+info['CFBundleIdentifier']+'" version="'+info['CFBundleShortVersionString']+'">ModelMonitor.component.pkg</pkg-ref>'
        '</installer-gui-script>')
    package = work/'ModelMonitor-macOS-arm64.pkg'
    run('productbuild', '--distribution', distribution_xml, '--resources', ui, '--package-path', work, package)
    archive_app = work/'ModelMonitor-macOS-arm64.zip'
    run('ditto', '-c', '-k', '--sequesterRsrc', '--keepParent', app, archive_app)
    for artifact in (package, archive_app):
        destination = output/artifact.name
        shutil.copy2(artifact, destination)
        (output/(artifact.name+'.sha256')).write_text(hashlib.sha256(destination.read_bytes()).hexdigest()+'  '+destination.name+'\n')
    standalone = output/'Model Monitor.app'
    if standalone.exists():
        standalone.rename(work/'previous-build.app')
    run('ditto', app, standalone)
    shutil.copy2(ROOT/'src/Start Model Monitor.command', output/'Start Model Monitor.command')
    print(json.dumps({'installer':str(output/package.name),'app':str(standalone),'work':str(work)}))


if __name__ == '__main__':
    main()
