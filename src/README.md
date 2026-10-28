# Model Monitor operating guide

For first-time installation, building the app and trusting its proxy certificate,
start with the [main README](https://github.com/Ooo0ption/ModelMonitor#installation).
This guide covers the app after setup.

## First launch with the complete package

The package includes mitmproxy and the helper's Python runtime. **Set up capture**
checks the runtime, local certificate and macOS SSL trust. Use **Prepare Local
Certificate** to create a CA on your Mac, then **Trust Certificate…** to review
and explicitly approve SSL trust for your current macOS account. This can affect
other apps using that trust domain. The installer never changes certificate trust.
You can return to this screen using **Capture setup** or **Finish setup**.

New CA files live in `CertificateAuthority` inside the data folder. An existing
selected CA is reused. Keep the private `mitmproxy-ca.pem` file private; the
public `mitmproxy-ca-cert.pem` is the file imported into Keychain. **Open in
Keychain** offers a manual alternative. Closing the app does not revoke trust.

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
**Copy** copies the displayed preview; the folder button reveals its saved
payload or summary file in Finder. **Refresh packets** reloads events saved since opening
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
flow. Astra and codex-auto-review records show **Summary-only storage**: original
prompt, generated text and tool contents were intentionally omitted. Other models
retain full payloads. Historical metadata-only records also show a notice when no
full body exists.

## Export and open data

| Control | Result |
| --- | --- |
| **Export CSV** | Saves the day's request/response summary files under `Exports/<date>/`. |
| **Open capture viewer** | Opens the live mitmweb view for the running proxy. |
| **Open saved traffic** | Creates an offline HTML index, retained JSONL events, body or summary files and connection metadata. Works after the proxy has stopped. |
| **Open data folder** | Opens the active data folder, including archives and diagnostic logs. |
| **Import saved flows** | Imports model summaries from a native mitmproxy flows file; it does not create historical full archives. |

Exports for long connections assemble their saved events across dates. Offline
HTML previews escape captured markup and do not execute captured scripts or
replay requests. Full entries contain the exact captured payloads; summary entries are labelled.

## Settings and data location

Stop the managed proxy before saving changes in **Settings**. Select the actual
desktop app, the `mitmweb` executable and the trusted public CA certificate.
The matching CA/private-key file must remain in the certificate folder.

New installations store data in `~/Library/Application Support/Model Monitor`.
Earlier ModelScope and GPT Model Trace profiles keep their existing folder until
you choose a new one. To move data, select an empty directory outside the `.app`
bundle and click **Save**. The app copies records, archives, exports and settings,
then switches to the new folder. The previous folder remains as a backup.
An internal certificate folder moves with the data and its selected path is
updated; an external selected certificate stays at its original location.
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
The sidebar distinguishes **Proxy is recording** from **Recording is not active**.
A missing recording plugin triggers a recording alert. Workers started by
version 1.4.1 or later stop the proxy if recording fails its continuous health
check; replacing the installed app does not unload the private runtime copy.
Single bursts, stopped proxies, stale incidents and recovered connections do not
produce a persistent warning. Each incident is shown once. `capture-health.json`
retains diagnostic timestamps and errors; `archive-health.json` records archive
readiness. `proxy.log` contains proxy process output.

## Retention and limits

Astra-family responses and `codex-auto-review` use summary-only storage. Summaries
keep model names, IDs, times, status, available token usage and byte/checksum
metadata; they omit prompts, generated text and tool contents. All other models,
including Luna, retain full request and response bytes. The response model is
authoritative. Requests wait in a durable temporary queue until that model is
known. Unknown or ambiguous traffic remains full. Shared WebSocket connections
are classified per response rather than dropped as a whole.

New archives use transparent gzip compression. The app can read both old `.trace`
and new `.trace.gz` files, and restores exact retained bytes for viewing/export.
Files rotate by Hong Kong date and roughly 64 MiB of stored data. No files are
automatically deleted. Full records and shared connection metadata can contain
credentials. Recording failures stop the proxy. Live flow retention in mitmweb
still holds full content and consumes memory.

Updating the app does not replace a running worker. Finish generation, then stop
and start the proxy to apply the new rules. The status note shows the active
policy. Historical archives, previous exports and recovery snapshots keep their
old content until separately compacted or removed.

Failed TLS handshakes cannot produce decrypted bodies. IP/TLS wire packets,
HTTP wire framing and WebSocket fragmentation/PING/PONG frames are not captured
by the application event interface. Full recording begins when the capture
worker runs; missing historical content cannot be reconstructed later.

For the archive format, integrity checks and recovery behavior, see
[Storage design](https://github.com/Ooo0ption/ModelMonitor/blob/main/STORAGE.md).
