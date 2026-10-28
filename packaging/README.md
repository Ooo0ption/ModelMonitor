# Build the complete macOS installer

Build on an Apple Silicon Mac with Apple's Swift command-line tools and
Python 3.9 or later. These are build dependencies; the resulting app includes
its own helper interpreter and mitmproxy runtime.

```sh
python3 -m venv build/venv
build/venv/bin/python -m pip install -r packaging/requirements.txt
build/venv/bin/python packaging/build_installer.py
```

The builder downloads the official mitmproxy archive pinned in `runtime.json`,
checks its SHA-256, preserves its bundled dependencies and includes license
notices. To reuse a cached copy, add `--mitmproxy-archive /absolute/path/to/archive.tar.gz`;
the same checksum check is required. Downloads go directly to the public source,
without using a capture proxy inherited from the build environment.

Outputs are under ignored `dist/`:

- `ModelMonitor-macOS-arm64.pkg`: installer for `/Applications/Model Monitor.app`.
- `ModelMonitor-macOS-arm64.zip`: the same complete app for manual installation.
- `Model Monitor.app`: complete local application bundle.
- `.sha256` files for checking the two distributable artifacts.

The package is unsigned; the app is ad-hoc signed and is not notarized. No
Developer ID signing identity is configured. Distribution to other Macs may
require a manual macOS security approval. Do not disable Gatekeeper globally.
The package does not include installation scripts, a user CA, private keys,
account credentials, settings or captured traffic. The installed runtime may
include its normal public root certificate bundle.

The installer copies files only. The user generates their own CA in the app and
explicitly approves SSL trust in their macOS user domain. Existing external
certificates and valid custom runtimes are retained. The app never generates a
replacement for an incomplete selected CA pair.

## Verify a build

```sh
python3 -m unittest discover -s tests -v
python3 tests/test_bundled_runtime.py "dist/Model Monitor.app"
codesign --verify --deep --strict "dist/Model Monitor.app"
pkgutil --expand-full dist/ModelMonitor-macOS-arm64.pkg build/package-check
```

The runtime test uses an isolated temporary profile, the packaged helper, a
generated local CA and synthetic HTTP traffic. It checks relocation recovery,
CA reuse and private-key permissions, archive contents and proxy shutdown.
It does not import or trust the test certificate, or restart the user's proxy.
For package verification, run the same test against the app in the expanded
package's `ModelMonitor.component.pkg/Payload/Applications` folder.

Build work, binaries and runtime data are ignored by Git. Publishing a GitHub
Release is a separate action; this builder does not publish or install anything.

## Release compatibility

The complete package requires Apple Silicon and macOS 14 or later. The bundled
Python framework in this release has a macOS 14 deployment target, even though
the earlier app shell declared macOS 13. Intel Macs are not supported.
Check the deployment targets of bundled Mach-O files when changing runtimes.

The PKG and app may each need approval in System Settings → Privacy & Security
→ Open Anyway after the first blocked launch. Managed Macs may prohibit this.
See [Apple’s guidance](https://support.apple.com/102445). Installing into
Applications may require administrator authorization. HTTPS capture also needs
the user’s explicit local CA trust; certificate-pinned clients may still reject it.
