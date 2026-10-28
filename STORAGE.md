# Storage design

## Data folder
The active folder is shown in Settings. New installations use
~/Library/Application Support/Model Monitor. Existing ModelScope and GPT Model Trace profiles remain in their earlier
Application Support folder until explicitly moved.

## Layout

```text
  records.sqlite3                 Lightweight model counts, times and IDs
  journals/*.jsonl                Lightweight model events for recovery
  Captures/YYYY-MM-DD/*.trace.gz  Compressed retained packets and summaries
  Captures/YYYY-MM-DD/*.trace     Legacy uncompressed archives
  Pending/*.trace                Durable temporary records awaiting classification
  Recovery/                      Explicit repair snapshots and backup archives
  capture-health.json             Model recorder diagnostics
  archive-health.json             Full recorder readiness and errors
  Exports/YYYY-MM-DD/traffic-*/   Offline viewer and full exports
  Exports/YYYY-MM-DD/packet-*/    Per-model-record viewer files
  settings.json                  Proxy launch preferences
  CertificateAuthority/          Locally generated CA and private key for new profiles
  Runtime/<worker-session>/      Private copy of recorder scripts used by that worker
```

## Capture scope
The recorder handles traffic routed through the proxy. Full-retention entries
for parsed HTTP save URLs, methods, versions, ordered/repeated headers,
trailers, status/reason, timing, errors, client/server connection metadata,
and exact received body bytes. Content-Encoding is preserved, so a gzip body
is still gzip in its .bin file. Chunks are persisted before the stream callback
returns the same bytes for forwarding. SSE chunks are saved before completion.
Full-retention WebSocket text and binary messages store the entire payload, time,
direction, opcode and message index. TCP/UDP payload events and DNS state are
also recorded if those protocols are exposed by the configured proxy mode.
CONNECT and connection/TLS success/failure events are included.

This captures the application data mitmproxy can parse/decrypt. It does not
capture unrelated direct traffic, TLS plaintext after a failed handshake,
network-level packets, HTTP wire framing, WebSocket original fragmentation,
or WebSocket PING/PONG frames (mitmproxy forwards but does not expose/store
those as message events). WebSocket close information is saved at end.

## Model retention policy

`src/retention.py` defines the policy. Astra-family names (for example,
`gpt-6-astra`, `gpt-6.1-astra`, and dated variants) and exact `codex-auto-review`
responses retain summaries. Other and unclassified traffic remains full.
Classification uses structured model fields, never model names mentioned in text.

Summary metadata includes model, IDs, timestamps, status and available numeric
usage, with counts, original byte sizes and SHA-256 values. It is not a generated
text summary. Original prompts, instructions, generated content and tool payloads
are omitted. HTTP summaries also omit authorization/cookie headers and URL query
strings. Shared WebSocket connection metadata stays available because one
connection may serve multiple models; it may still contain credentials.

The **response model** controls retention. A request for Astra whose response
reports Luna is retained in full. Outgoing requests are fsynced under `Pending/`
until a response can be associated safely. Multiple concurrent pending requests
remain full when association is ambiguous. Binary/unrecognized messages and
requests that end without a classifiable response remain full. Stranded pending
records are recovered as unclassified full content after a crash. The temporary
queue is not a permanent second copy and is removed after a durable commit.

WebSockets are classified by response and output-item IDs, not by connection.
HTTP Responses/chat-completions/conversation endpoints are probed as JSON or SSE,
including gzip and deflate bodies. Unrecognized endpoints or encodings and
unclassifiable responses remain full. Probing uses bounded memory; conservative
fallback can therefore retain some excluded-model data if it cannot be identified
safely. This prioritizes preserving other models. Streaming delta contents are
omitted for summary models; lifecycle events and an aggregate final summary stay.
An orderly shutdown writes an interrupted summary when completion was not seen.
A crash can leave only the already committed lifecycle summary.

The separate SQLite model index and journals still count every model. Filtering
changes disk persistence only: requests/responses forwarded to clients are not
modified. The live mitmweb viewer can still hold complete flows in memory.

## Durability and retention
The recorder creates a unique session ID on each start. Files are grouped by
Hong Kong date and rotated after roughly 64 MiB of stored bytes (a single large event may
exceed that threshold). Nothing is automatically deleted or overwritten.
Every retained archive record is appended and fsynced. New files use gzip
compression with a sync flush before fsync, so complete records are readable
while capture is running. Compression does not change retained payload bytes. A write/serialization failure
stops the proxy, writes a diagnostic when possible, and exits with code 74.
Previously committed records survive a proxy crash; data not yet received
or not yet exposed by mitmproxy cannot be saved. Readers report and ignore
an incomplete trailing record, and refuse checksum-corrupted records.
Rotation writes a new flow context for each continuing flow, avoiding
repeated copies of all preceding WebSocket messages.

## Binary format, version 1
New `.trace.gz` files wrap the following format in gzip. Legacy `.trace` files
use it directly. Readers verify each inner record checksum, including when a
live gzip file has not yet received its closing footer.

File header: the bytes GPTTRACE followed by 0x01 and a newline.
Each record contains:

```text
  uint32 big-endian JSON-envelope byte length
  uint64 big-endian payload byte length
  UTF-8 JSON envelope
  retained payload bytes (original full bytes or a generated metadata summary)
  32-byte SHA-256 digest of envelope bytes followed by payload bytes
```

The envelope includes version, session, monotonic sequence within the session,
Unix timestamp, event kind and flow ID when applicable. Binary values in
metadata use {"$bytes_base64":"..."}. HTTP body events carry their byte offset;
full payload bytes remain binary rather than growing by base64 encoding.
Summary envelopes carry `retention: "summary"`; summary JSON includes
`_capture_summary` with byte counts and checksums of omitted originals.

## Viewing and export
Open saved traffic uses the selected date to identify flows, then includes
their saved events from every date. This preserves long connections crossing
midnight. It writes a fresh export directory with an offline index.html,
events.jsonl (retained payload_base64 plus exact envelope), per-flow header
JSON and body .bin files, messages.jsonl, and complete state.json metadata.
Text WebSocket messages also have a full payload_text field for direct reading;
payload_base64 remains the authoritative lossless representation.
The HTML preview escapes captured markup, disallows external content/scripts,
and limits preview length only. Linked full entries are never truncated. Summary entries explicitly indicate
that the original content was intentionally omitted; HTTP summaries have their
own `.summary.json` files instead of placeholder empty body files.
The browser never replays captured traffic. No external service receives it.
For command-line export, use controller.py export-traffic --date YYYY-MM-DD.

## Per-record viewing
Click a model row to show its own saved request/response content. The helper
command is controller.py packet-detail --mode requests|responses --id RECORD_ID.
For WebSockets, response IDs and output item IDs select individual messages.
A request is paired only when one request is pending on that flow; concurrent
ambiguous requests are not guessed. Unidentifiable events remain in the full
archive/export. HTTP details include that flow's complete saved body and state.
The response_flows SQLite table remembers response-to-flow links for new events;
older records can recover those links from their existing metadata journals.
Previews are bounded; full-retention raw payload files remain complete. Summary-only records show a
notice rather than implying that their original bodies are available. packet-* files are
derived views and can be recreated from the original archives. No original
archive is changed. Old metadata-only records cannot produce missing bodies.

## Historical data and cleanup

No automatic age limit or deletion is applied. Updating the app and restarting
the worker affects new traffic; it does not rewrite old archives, exports or
recovery snapshots. `tools/compact_captures.py` prepares a filtered and compressed
copy in a new folder, skips the active session, validates every record, checks
input hashes before/after, and verifies preservation of identified non-summary
WebSocket payloads and non-summary HTTP/transport body bytes. It writes
`compaction-report.json` and never replaces or deletes inputs.

A replacement must match that report's input hashes and preserve archives
created after planning. Keep the app closed during replacement so packet readers
do not observe a partial switch. The model-count database is independent and
must not be rebuilt or cleared as part of this operation. Summary loss is
intentional and cannot be reversed after original files/backups are removed.

`Exports/` contains derived files that can usually be recreated from retained
archives. Exports made before filtering may still contain full excluded-model
bodies; remove those separately when no longer needed. `Recovery/` and project
`backups/` can hold large duplicate native flow snapshots and full archives.
Those are independent recovery copies, not active recording data. Inspect and
remove only obsolete snapshots after validating recovery; do not delete the
whole data folder, model index, journals, or certificate/private-key files.
Running the explicit restore tool may create full recovery copies again.

## Validated cleanup command

Stop the managed proxy, then run `python3 tools/cleanup_storage.py`, or double-click
`dist/Clean stored traffic.command` in the local checkout. The command filters
all closed archives using the current model rules, verifies preserved payloads,
and checks recovery snapshots against the original canonical archive. Stopped
sessions are included even when the last archive-health file retains their ID.

It removes only the replaced full archives, recovery directories whose HTTP
byte ranges, WebSocket payloads and saved model indexes have been verified,
and regenerable `Exports/*/packet-*` preview caches. Unclassified traffic stays
full. Counts, timestamps, journals, settings, certificates and source/app backups
are preserved. If a backup contains unique data, cleanup stops before deletion.

The switch is validated before original files are deleted, with rollback on a
replacement validation failure. A small proof and transaction receipt remain in
`Cleanup/`. If interrupted, keep that receipt and any remaining original files
for inspection; do not manually empty its transaction directory. The command
refuses a running proxy and locks managed startup during replacement. Source
hashes must still match the plan. If any files changed, prepare a fresh plan.

## TLS diagnostics
capture-health.json stores a proxy session ID and per-host failure timestamps,
latest error, incident start and recovery timestamp. The full archive still
retains the original TLS events. Recent repeated unresolved failures can show
one alert per incident; stale warnings, stopped proxies, short bursts and
recovered connections do not. A successful handshake clears failures for that
host only. Alert IDs remembered by the desktop UI are bounded to the last 100.

## Privacy and moving data
No fields are redacted in full archives or exports. These files contain the
user's prompts, responses and any credentials traversing the proxy. Archive
folders are 0700, files are 0600, and exports have the same permissions. This
is local access control, not application-level encryption. Use normal macOS
disk protection for encryption at rest if needed.
Changing Data folder requires stopping the managed proxy. The app copies all
archives, journals, exports and settings, backs up SQLite consistently, changes
the location preference, and leaves the old directory as a backup. A target
must be empty. With a large archive, copying and exporting can take time.
The locally generated CA is copied with the data, and its selected path follows
the new folder. An external selected CA is left in place. The old backup may
therefore also contain a private key and should be protected like the active data.
For long folder paths, the private worker control socket lives in a mode-0700
per-user folder under `/private/tmp`; captured traffic always stays in the chosen
data folder. The socket is removed when the worker exits.

## Capture availability
Capture requires a proxy worker that loads the recording addon and retention policy.
Each worker runs an immutable copy of the recording scripts in the data folder,
so replacing or moving the installed app cannot remove its active addon. Each
addon load has a distinct archive session, including reloads within one worker.
Status checks both recorder health and the proxy's loaded scripts. A running
proxy with no recorder is reported as an error. New workers also check recording
continuously and stop forwarding when recording cannot be verified twice in a row.
Opening a newer desktop window does not replace an already-running worker.
After finishing active work, stop and start that managed proxy from the new
app. Existing statistics remain available, but missing historical bodies
cannot be reconstructed.

## References
- [mitmproxy event hooks](https://docs.mitmproxy.org/stable/api/events.html)
- [mitmproxy protocol support](https://docs.mitmproxy.org/stable/concepts/protocols/)
- [Streamed-body retention](https://docs.mitmproxy.org/stable/concepts/options/#store_streamed_bodies)

## JSON previews
Large JSON prefixes are indented without requiring a complete closing structure.
Quoted content and escapes are preserved, and no missing closing tokens are
invented. Formatting is also bounded to 64 KiB. A cut UTF-8 character at the
preview boundary does not turn valid text into a binary preview. Original
captured files are never changed. The native text reader wraps long lines.
