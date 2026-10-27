# Model Monitor

A native macOS app for inspecting model activity captured through a local proxy.
View daily model counts and request times, open individual request/response
records, and retain complete captured application traffic on your Mac.

## Features

- Start a managed local proxy and launch the selected GPT desktop app with its proxy settings.
- Count request models and response models separately, with timestamps in Hong Kong time.
- Open a model record to inspect its associated request, response and connection events.
- Read indented JSON previews and reveal complete original payload files.
- Save HTTP requests/responses, WebSocket messages and other application events exposed by mitmproxy.
- Export model summaries as CSV or browse full saved traffic offline.
- Choose a data folder in Settings. Closing the window keeps the proxy recording.

Model counts reflect captured model fields. One chat turn can produce several
requests; a reported model name does not verify the underlying model weights.

## Requirements

- Apple Silicon Mac running macOS 13 or later.
- Apple's command-line developer tools, including `swiftc`.
- Python 3.9 or later available at `/usr/bin/python3` for the desktop helper.
- An installed `mitmweb` executable from mitmproxy. The capture integration has
  been tested with mitmproxy 12.3.1.
- A desktop application to launch and an existing mitmproxy CA trusted by the
  application whose traffic you want to inspect.

The native UI uses SwiftUI and AppKit. The controller and unit tests use Python's
standard library; the capture addon runs inside mitmproxy. This is an independent
utility, not an official OpenAI application.

## Build and run

```sh
git clone https://github.com/Ooo0ption/ModelMonitor.git
cd ModelMonitor
./src/build.command
open "dist/Model Monitor.app"
```

The build creates an ad-hoc-signed `dist/Model Monitor.app` and a
`dist/Start Model Monitor.command` shortcut. You can copy the app to Applications.
The shortcut should stay beside the app. Installers and previous release builds
are not included in this repository.

1. Open **Settings** to select the desktop app, `mitmweb`, CA certificate and data folder.
2. Click **Start Proxy & GPT**. If the selected app is already open, finish active work
   before choosing to restart it with the proxy.
3. Send a message, then view the daily counts or click a model record for its packets.
4. Use **Open saved traffic** for the complete offline export, or **Export CSV** for model summaries.

The proxy defaults to `127.0.0.1:18741`; its viewer uses `127.0.0.1:18742`.
An available pair is selected if those ports are occupied. The app applies proxy
settings to the desktop app it launches; it does not change the system proxy.
Other clients explicitly configured to use this proxy can also be captured.
TLS verification remains enabled, and capture depends on the client trusting the CA.

## Data storage and capture scope

New installations use `~/Library/Application Support/Model Monitor`.
Existing profiles retain their previous data folder. Settings can move the data
after the managed proxy has stopped, leaving the previous folder as a backup.

The proxy saves complete application-level traffic it can parse and decrypt,
including prompts, responses, headers, cookies and credentials. Capture data
stays outside the app bundle and is excluded from Git. Archives rotate by date
and size; there is no automatic deletion. Recording failures stop the proxy.

Only traffic routed through this proxy is captured. Failed TLS handshakes have
no decrypted body to save. The archive is not an IP/TLS packet capture, and
WebSocket wire fragmentation and PING/PONG control frames are not exposed by
mitmproxy's message interface. Earlier metadata-only records cannot provide
missing historical bodies. Previews are bounded; linked payload files retain
the complete captured bytes.

See [STORAGE.txt](STORAGE.txt) for archive format, recovery and correlation
details, and [src/README.txt](src/README.txt) for operating notes.

## Source layout

| Path | Purpose |
| --- | --- |
| `src/ModelScope.swift` | Native window, model activity, packet viewer and settings |
| `src/controller.py` | Proxy lifecycle, launch settings, data management and exports |
| `src/capture_addon.py` | Loads full capture and lightweight model recording |
| `src/full_capture.py` | Streaming HTTP, WebSocket and transport event recording |
| `src/traffic_archive.py` | Durable archives, integrity checks and offline exports |
| `src/packet_details.py` | Per-record packet correlation and formatted previews |
| `src/capture_diagnostics.py` | TLS diagnostics and alert eligibility |
| `src/store.py` | SQLite model summaries and deduplication |
| `src/model_report.py` | Native mitmproxy flow parsing and model extraction |
| `src/build.command` | Native build and local signing |
| `tests/` | Synthetic unit tests and local proxy integration checks |

Legacy internal identifiers and archive headers are retained for compatibility.

## Validation

Run the unit tests from the repository root:

```sh
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

The optional end-to-end capture check starts temporary local servers and a
separate test proxy. It currently expects `mitmweb` at `/opt/homebrew/bin/mitmweb`:

```sh
python3 tests/test_proxy_capture.py
```

Tests generate synthetic traffic and temporary certificates; no real captures
or credentials are included. Build outputs, previous versions, traffic archives,
exports, databases, logs, certificates and local settings are ignored by Git.

## License

[MIT](LICENSE).
