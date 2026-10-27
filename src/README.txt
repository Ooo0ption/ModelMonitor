Model Monitor
macOS desktop app for your local mitmproxy and GPT installation.

Use the app
1. Open Model Monitor.app. You can move the app to your Applications folder.
2. Click Start Proxy & GPT. The app starts its own mitmweb instance, then launches GPT with that proxy.
3. If GPT is already running, choose Restart with proxy after finishing your current work. Choosing Show existing window keeps that process's existing proxy settings.
4. Send a message in GPT. The app saves the request model and response model automatically and refreshes the record view every four seconds.
5. Choose a date and Request model or Response model. Export CSV saves the selected day's timestamps and counts.
   Click a timestamp row to open its saved packets inside the app. Switch between Request, Response and Connection, select an event, and copy the shown text or reveal its complete file in Finder. Refresh packets includes events saved since the page opened. The back button returns to the activity list.
6. Click Open saved traffic to view and export full requests, responses and messages for that date, even after the proxy has stopped.
After upgrading from an older version, stop its proxy and start the proxy from this version. Full recording begins then; earlier metadata-only captures cannot be recovered as full packets.

Optional shortcut
Double-click Start Model Monitor.command, kept beside the app. It opens the app and starts the same one-click flow automatically. Starting twice reuses the managed proxy; it does not create a second proxy on that port.

Ports and networking
Default HTTP proxy: 127.0.0.1:18741
Default capture viewer: 127.0.0.1:18742
If either port is occupied, the app selects a free pair and displays the active proxy port. Existing proxies, including one on 8080, are left alone. Only GPT launched here receives proxy environment variables and the Chromium proxy argument. System proxy settings are not changed.
The existing mitmproxy CA is reused. The app verifies TLS trust and provides the CA bundle to GPT's processes; it does not disable certificate validation. Settings lets you choose the GPT app, mitmweb executable, CA file and data folder.
There is no persistent certificate-warning footer. A failed macOS CA trust check shows an alert when launching GPT. During capture, an alert appears only while the proxy is running and a destination has at least three failed client TLS handshakes over at least ten seconds within the last two minutes, with its latest failure in the last minute and no later successful handshake to that destination. Each incident is shown once. Success clears the incident, and a new failure incident may alert again. Diagnostics preserve the destination, actual TLS error and timestamps in capture-health.json; not every TLS failure means the certificate is untrusted. Old text-only warnings are not treated as current incidents.

Records
New installations use ~/Library/Application Support/Model Monitor for data. Existing installations retain their ModelScope or GPT Model Trace data folder until you choose a new folder in Settings. The active path is shown in Settings and can be opened from the sidebar.
To change it, stop this app's proxy, open Settings, choose an empty folder (or click Use standard macOS folder), then Save. The app copies the database, journals, settings and exports, switches to the selected folder, and leaves the old folder as a backup. The folder must be outside the .app bundle. The selected path is remembered in ~/Library/Preferences/local.yijia.modeltrace-location.json.
Full traffic is saved automatically to Captures/YYYY-MM-DD/*.trace, even when the desktop window is closed. Every exposed application flow is included: HTTP request/response headers, URLs, body bytes, errors and trailers, WebSocket text/binary messages, and exposed TCP/UDP/DNS events. Headers and bodies are retained without redaction, including prompts, answers, Authorization headers and cookies. The separate model journals and database remain lightweight summaries.
Files rotate by Hong Kong date and roughly 64 MiB, without deleting older files. Each complete record is checksummed and fsynced. The reader recovers all committed records after an interrupted write and reports an incomplete tail. Archive directories are 0700 and files 0600. A recording or disk failure stops the proxy to prevent unrecorded traffic. Complete traffic consumes disk space indefinitely; mitmweb also retains live flows in memory.
Request counts are grouped by the sent model and sending date. Response counts use the returned model and first captured date, deduplicated by response ID. One chat turn can involve several responses. Model names in metadata or prompt text are excluded. A model field is not proof of the underlying model weights.
Existing Application Support records remain available when this version opens. Use Import saved flows for other captures. Records not captured by this proxy, or cleared before being saved, cannot be recovered.
Dates use Asia/Hong_Kong. Request and response counts are separate and can differ. HTTP JSON/SSE model summaries are updated when the body finishes; their full bytes are saved incrementally before then. WebSocket model summaries and full message payloads are saved on each message event.
Open saved traffic creates an offline HTML index plus full JSONL events, raw body .bin files, message JSONL files and flow metadata under Exports/<date>/traffic-<timestamp>. Long flows are assembled from all saved dates. HTML previews are limited for readability; linked files retain all captured content. TLS failures have no decrypted body to save. HTTP wire framing and WebSocket fragmentation/PING/PONG frames are outside mitmproxy's application event interface; the archive is not a packet-level PCAP.
The model row detail page reads the same archives. WebSocket responses are linked by response and item IDs; a request is paired only when one request is pending on that connection. Ambiguous matches are explicitly omitted from the row view, with the complete connection still available through Open saved traffic. HTTP records show the exchange for their flow. JSON previews remain indented even when shortened; long lines wrap and new messages start at the top. Both input sampling and formatted previews are limited to 64 KiB (4 KiB for binary hex); derived files under Exports/<date>/packet-* preserve complete captured payloads. Gzip and deflate bodies have decoded previews while their files retain the original compressed bytes. Historical metadata-only records display saved metadata and a notice explaining that full bodies are unavailable.
Closing or quitting the desktop app leaves its proxy running. Use Stop proxy to stop the managed proxy. This does not stop another proxy that you started elsewhere.

Requirements
Requires Apple Silicon, macOS 13 or later, Python 3 at /usr/bin/python3 and an installed mitmweb executable. This is a local, ad-hoc-signed app, not a notarized App Store distribution. It is not an official OpenAI application.

Source and rebuild
ModelScope.swift: native SwiftUI/AppKit window and GPT launch flow.
controller.py: local proxy supervisor, startup settings, history import and export.
capture_addon.py: loads full capture and the lightweight model recorder.
full_capture.py: full streaming and message capture hooks.
traffic_archive.py: checksummed binary archives and offline export.
packet_details.py: correlates each model row with its saved packets.
capture_diagnostics.py: recent per-host TLS incidents and alert thresholds.
store.py: daily SQLite aggregation and deduplication.
model_report.py: dependency-free parser for native mitmproxy flows files.
build.command: builds and locally signs the app with Apple's command-line tools.
All application data is kept outside the app bundle, so rebuilding does not erase it.

Validation
Verified with the installed mitmweb 12.3.1: actual WebSocket capture, deduplication, metadata exclusion, port conflict handling, capture viewer authentication, persistent records after stop, and CSV export. The native one-click launch path was exercised with a temporary probe app to verify proxy arguments and CA environment without interrupting the active GPT conversation. Actual GPT restart is deferred to your explicit choice in the app.

Reference
mitmproxy addon event hooks: https://docs.mitmproxy.org/stable/api/events.html
mitmproxy protocol boundaries: https://docs.mitmproxy.org/stable/concepts/protocols/
mitmproxy streamed-body retention: https://docs.mitmproxy.org/stable/concepts/options/#store_streamed_bodies
OpenAI desktop troubleshooting: https://learn.chatgpt.com/docs/reference/troubleshooting
The proxy launch integration is based on the local app and its Electron runtime; the OpenAI troubleshooting page does not document a guarantee for this interception setup.
