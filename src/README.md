# Model Monitor operating guide

For first-time installation, building the app and trusting its proxy certificate,
start with the [main README](https://github.com/Ooo0ption/ModelMonitor#installation).
This guide covers the app after setup.

## Start and stop

Click **Start Proxy & GPT** to start the managed proxy and launch the desktop app
selected in Settings. If that app is already open, choose **Restart with proxy**
after finishing active work. **Show existing window** keeps the running process's
existing proxy settings.

The separate **Start proxy** and **Launch GPT** controls perform each step
individually. Closing the window or quitting Model Monitor leaves the proxy
running. **Stop proxy** stops only the instance managed by Model Monitor and
disconnects clients using it; saved records remain on disk.

The optional `Start Model Monitor.command` shortcut opens the same startup flow.
Keep it beside `Model Monitor.app`. Starting twice reuses the managed proxy.

## Read model activity

1. Select a date or click **Today**. All dates and timestamps use **Asia/Hong_Kong**.
2. Select **Request model** to see models sent in requests, or **Response model**
   to see models reported by responses.
3. Choose **All models** or filter by model name.
4. Click a timestamp row to open its saved packets.

The activity view refreshes about every four seconds. Request and response
counts can differ, and a chat turn can contain several requests. Streaming
response events are deduplicated by response ID. Model names found only inside
prompt text or unrelated metadata do not count as model calls.

## Inspect a packet

Switch between **Request**, **Response** and **Connection**, then select an event.
**Copy** copies the displayed preview; the folder button reveals its complete
payload file in Finder. **Refresh packets** reloads events saved since opening
the page. **Model activity** returns to the list.

JSON previews are indented even when shortened, and long lines wrap. Input
sampling and formatted text are bounded to 64 KiB; binary hex previews use
4 KiB. **Preview shortened** describes the display, not the original archive.
Gzip and deflate bodies have decoded previews while the saved original bytes
retain their compression.

WebSocket records are correlated using response and output-item IDs. A request
is paired only when one request is pending on that connection; ambiguous matches
are omitted from the per-record view. The full connection remains available
through **Open saved traffic**. HTTP records show the saved exchange for their
flow. Historical metadata-only records show a notice when no full body exists.

## Export and open data

| Control | Result |
| --- | --- |
| **Export CSV** | Saves the day's request/response summary files under `Exports/<date>/`. |
| **Open capture viewer** | Opens the live mitmweb view for the running proxy. |
| **Open saved traffic** | Creates an offline HTML index, full JSONL events, original body files and connection metadata. Works after the proxy has stopped. |
| **Open data folder** | Opens the active data folder, including archives and diagnostic logs. |
| **Import saved flows** | Imports model summaries from a native mitmproxy flows file; it does not create historical full archives. |

Exports for long connections assemble their saved events across dates. Offline
HTML previews escape captured markup and do not execute captured scripts or
replay requests. Linked files contain the complete captured payloads.

## Settings and data location

Stop the managed proxy before saving changes in **Settings**. Select the actual
desktop app, the `mitmweb` executable and the trusted public CA certificate.
The matching CA/private-key file must remain in the certificate folder.

New installations store data in `~/Library/Application Support/Model Monitor`.
Earlier ModelScope and GPT Model Trace profiles keep their existing folder until
you choose a new one. To move data, select an empty directory outside the `.app`
bundle and click **Save**. The app copies records, archives, exports and settings,
then switches to the new folder. The previous folder remains as a backup.
**Use standard macOS folder** selects the current default.

The selected location is remembered in
`~/Library/Preferences/local.yijia.modeltrace-location.json`. Internal preference
names remain stable for compatibility.

## Networking and diagnostics

The proxy defaults to `127.0.0.1:18741`; its viewer uses `127.0.0.1:18742`.
If either port is occupied, an available pair is selected and the active proxy
port is shown in the sidebar. System proxy settings are unchanged. Only clients
routed through this proxy are captured.

The app checks macOS trust for the selected CA before launching the target app,
and supplies proxy and CA environment settings to that process. It keeps TLS
verification enabled. Follow the certificate setup and troubleshooting steps in
the main README if a client rejects the proxy's certificate.

During capture, recent repeated unresolved TLS failures can trigger an alert.
Single bursts, stopped proxies, stale incidents and recovered connections do not
produce a persistent warning. Each incident is shown once. `capture-health.json`
retains diagnostic timestamps and errors; `archive-health.json` records archive
readiness. `proxy.log` contains proxy process output.

## Retention and limits

Every parsed application flow exposed by the managed proxy is archived without
a hostname or model filter. This includes request/response contents, prompts,
headers, cookies and credentials. Nothing is redacted. Files rotate by Hong Kong
date and roughly 64 MiB without automatic deletion. Recording failures stop the
proxy rather than forwarding unsaved traffic. Live flow retention in mitmweb
also consumes memory.

Failed TLS handshakes cannot produce decrypted bodies. IP/TLS wire packets,
HTTP wire framing and WebSocket fragmentation/PING/PONG frames are not captured
by the application event interface. Full recording begins when the capture
worker runs; missing historical content cannot be reconstructed later.

For the archive format, integrity checks and recovery behavior, see
[Storage design](https://github.com/Ooo0ption/ModelMonitor/blob/main/STORAGE.md).
