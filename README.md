# Model Monitor

A native macOS app for viewing model calls captured through a local proxy.
See daily model counts and timestamps, inspect request/response packets, and
keep the complete captured traffic on your Mac.

**Start here:** [Install](#installation) → [Set up the certificate](#2-generate-and-trust-the-proxy-certificate) → [Launch and verify](#5-start-capturing-and-check-the-result).

This repository currently provides source code, not a downloadable installer or
GitHub Release. The steps below build a local `Model Monitor.app`.

## Installation

### 1. Install the prerequisites

The app targets **Apple Silicon and macOS 13 or later**. Building requires
Apple's command-line developer tools and Python 3.9 or later at
`/usr/bin/python3`. The monitored desktop app must already be installed.

Open Terminal and install Apple's tools if they are not already present:

```sh
xcode-select --install
```

Wait for the installer to finish, then check:

```sh
xcrun --find swiftc
/usr/bin/python3 --version
```

If Python is missing or older than 3.9, install or update the compatible Apple
developer tools before continuing. This app currently uses that fixed Python
path; installing a different interpreter elsewhere does not change it.

Install [Homebrew](https://brew.sh/) if needed, following the installer’s PATH
instructions. Current Homebrew support may require a newer macOS than the app
itself. For older systems, consult the
[mitmproxy installation options](https://docs.mitmproxy.org/stable/overview/installation/).

Install mitmproxy, which provides `mitmweb` and `mitmdump`:

```sh
brew install --cask mitmproxy
command -v mitmweb
mitmweb --version
```

Keep the path printed by `command -v mitmweb`; you will select it in Settings.
It is normally `/opt/homebrew/bin/mitmweb` on Apple Silicon. The capture
integration has been tested with mitmproxy 12.3.1.
See the [official macOS installation instructions](https://docs.mitmproxy.org/stable/overview/installation/).

### 2. Generate and trust the proxy certificate

Model Monitor needs a local mitmproxy certificate authority (CA) to decrypt
proxied HTTPS connections. Generate the files before starting the app's proxy.
If both files below already exist, reuse them and proceed to the trust check:

```sh
ls "$HOME/.mitmproxy/mitmproxy-ca-cert.pem" "$HOME/.mitmproxy/mitmproxy-ca.pem"
```

For a fresh setup, run:

```sh
mitmdump --listen-host 127.0.0.1 --listen-port 18749 \
  --set confdir="$HOME/.mitmproxy"
```

Once it reports that it is listening, press **Control-C** to stop this setup
instance. If port 18749 is occupied, use another unused high port. The managed
proxy started later by Model Monitor uses its own ports.

Mitmproxy creates a CA on first use. The two files have different purposes:

| File | What to do with it |
| --- | --- |
| `~/.mitmproxy/mitmproxy-ca-cert.pem` | Public CA certificate: import this into Keychain Access and select it in Model Monitor. |
| `~/.mitmproxy/mitmproxy-ca.pem` | CA plus private key: keep this private and in the same folder so the proxy can use it. |

#### Local storage and the scope of trust

In the default setup above, mitmproxy generates the CA and private key on your
Mac and stores them in `~/.mitmproxy`. The local mitmproxy process reads the
private key to sign certificates for proxied connections. The setup does not
upload that key to mitmproxy's developers or to this repository; installing and
trusting the certificate are local operations. Each newly generated CA is
unique, rather than a shared key supplied to every user.
See [mitmproxy's certificate guide](https://docs.mitmproxy.org/stable/concepts/certificates/).

The public certificate alone cannot sign new certificates. Protect
`mitmproxy-ca.pem`, which contains the private key: do not share it, commit it,
or include it in exported support files. Restrictive file permissions such as
`600` help prevent access by other ordinary accounts, but do not protect it from
malware running with your own account's permissions or administrator access.

**System-keychain SSL trust is broader than the app being monitored.** Other
clients using the same trust settings may also accept certificates issued by
this CA. If someone obtains its private key and can intercept or redirect your
connections, they may be able to impersonate HTTPS sites for those clients.
The proxy's localhost binding and upstream TLS checks do not narrow this CA
trust to GPT alone.

Stopping the proxy or quitting Model Monitor **does not revoke CA trust**.
When you no longer need this setup, remove its custom trust and imported CA
from the keychain where you installed it, using Keychain Access. Change only
the certificate belonging to your proxy. See
[Apple's certificate trust settings](https://support.apple.com/guide/keychain-access/change-the-trust-settings-of-a-certificate-kyca11871/mac).

Trust only a CA you control. Captured traffic also contains sensitive data;
keep both private keys and real captures out of GitHub. This repository ignores
certificate/key files and capture outputs by default.

#### Trust the CA using Keychain Access

1. Open **Keychain Access** using Spotlight and select the **System** keychain.
2. In Finder, use **Go → Go to Folder…** and enter `~/.mitmproxy`.
3. Drag **mitmproxy-ca-cert.pem** into the System keychain. Authenticate if macOS asks.
4. Find the imported **mitmproxy** certificate, double-click it, and expand **Trust**.
5. Set **Secure Sockets Layer (SSL)** to **Always Trust**, close the certificate
   window, and confirm the change when prompted.

Apple documents [adding a certificate](https://support.apple.com/guide/keychain-access/add-certificates-to-a-keychain-kyca2431/mac)
and [changing its trust settings](https://support.apple.com/guide/keychain-access/change-the-trust-settings-of-a-certificate-kyca11871/mac).

Alternatively, this command installs the same public CA certificate into the
System keychain with SSL trust. It requires your Mac administrator password;
you do not need to use both methods:

```sh
sudo security add-trusted-cert -d -r trustRoot -p ssl \
  -k /Library/Keychains/System.keychain \
  "$HOME/.mitmproxy/mitmproxy-ca-cert.pem"
```

Verify the selected CA with the same local trust check used by Model Monitor:

```sh
security verify-cert \
  -c "$HOME/.mitmproxy/mitmproxy-ca-cert.pem" \
  -p ssl -l -L -q
```

An exit status of **0** means this check passed; quiet mode may print nothing.
A failure means the certificate or its trust settings still need attention.
If you chose a different certificate folder, use that exact file in the command
and in Model Monitor. The app also supplies CA environment settings when it
launches the selected desktop app; it does not disable TLS verification.

### 3. Build and install Model Monitor

```sh
git clone https://github.com/Ooo0ption/ModelMonitor.git
cd ModelMonitor
./src/build.command
open "dist/Model Monitor.app"
```

The build creates:

- `dist/Model Monitor.app` — the locally built, ad-hoc-signed application.
- `dist/Start Model Monitor.command` — an optional shortcut that starts the app's
  **Start Proxy & GPT** flow; keep it beside the app.

To install the application for regular use, open the output folder and drag
**Model Monitor.app** into **Applications**:

```sh
open dist
```

Then launch **Model Monitor** from Applications. If you also want the shortcut,
copy it beside the app. This local build is not a notarized App Store package.

### 4. Configure the app

Open **Settings** in Model Monitor. The managed proxy must be stopped before
saving settings.

| Setting | What to select |
| --- | --- |
| **GPT application** | The actual installed desktop `.app` you want to launch, such as `/Applications/ChatGPT.app` or `/Applications/Codex.app`. Use **Choose…** if its location differs. |
| **mitmweb** | The executable path returned by `command -v mitmweb`. |
| **Proxy certificate** | Your trusted `~/.mitmproxy/mitmproxy-ca-cert.pem` file. The matching `mitmproxy-ca.pem` must remain beside it. |
| **Data folder** | Keep the default, `~/Library/Application Support/Model Monitor`, or select an empty folder outside the `.app`. |
| **Proxy port** | Default: `18741`. |
| **Capture viewer port** | Default: `18742`; it must differ from the proxy port. |

Use **Choose…** to select files and folders, then click **Save**. When entering
paths manually, use their full absolute paths instead of `~`.

### 5. Start capturing and check the result

1. Click **Start Proxy & GPT**.
2. If the selected desktop app is already running, finish active work and choose
   **Restart with proxy**. **Show existing window** does not change that process's
   existing proxy settings.
3. Check that the sidebar says **Proxy is running** and **GPT is running**.
4. Send a new message in the desktop app. The activity list refreshes about every
   four seconds; check the selected date and choose **All models** if needed.
5. Switch between **Request model** and **Response model**, then click a timestamp
   to inspect its **Request**, **Response** and **Connection** packets.

Use **Open capture viewer** for the live proxy view, **Open saved traffic** for
full offline exports, or **Export CSV** for model counts and timestamps. Closing
Model Monitor keeps recording; **Stop proxy** stops its managed proxy.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| `mitmweb` or `brew` is not found | Finish the Homebrew PATH setup and reopen Terminal. Use `command -v mitmweb` and select that executable in Settings. |
| The CA file is missing | Complete the certificate-generation step before starting Model Monitor's proxy. |
| `mitmproxy-ca.pem is missing` | The selected certificate folder must contain the matching CA/private-key file, not only the public certificate. Use the original folder. |
| **Proxy certificate needs attention** or `tls alert certificate unknown` | Run the trust check above for the exact certificate selected in Settings. Confirm its SSL trust in Keychain Access, then completely quit and relaunch the target app through Model Monitor. Do not solve this by turning off TLS verification. |
| Trust check succeeds but the target still rejects TLS | The client may use a separate trust store or certificate pinning. Model Monitor cannot guarantee interception of every app; macOS trust alone does not prove that every client accepts this CA. |
| No new model records appear | Check the sidebar, date and model filter. Relaunch the target with **Restart with proxy**, then send a new message. Open the live viewer to check whether requests reach the proxy. Older requests sent directly are not captured retroactively. |
| The sidebar shows a different port | A preferred port was occupied. Use the active port shown in the sidebar when configuring another client. |
| **Save** in Settings is disabled | Stop the managed proxy first, then change and save the settings. |
| A large packet says **Preview shortened** | Only the displayed preview is bounded. Use the folder button to reveal the complete saved payload. |

For diagnostic logs, click **Open data folder** and inspect `proxy.log`.
Some failures are specific to a destination or client; a TLS failure is not
always a missing certificate. The app reports recent repeated unresolved TLS
incidents instead of displaying stale warnings permanently.

## What is captured and where it is saved

Only traffic routed through this proxy is captured. Model Monitor configures
the desktop app it launches, not the system proxy. Other clients explicitly
configured to use its active proxy address can also be captured.

HTTP requests/responses, WebSocket messages and other application events exposed
by mitmproxy are saved locally. Payloads include prompts, responses, headers,
cookies and credentials without redaction. Failed TLS handshakes have no
decrypted body to save. This is an application traffic archive, not an IP/TLS
packet capture; WebSocket wire fragmentation and PING/PONG frames are outside
the message interface.

New installations use `~/Library/Application Support/Model Monitor`; existing
profiles retain their previous folder. Archives rotate by date and size with
**no automatic deletion**. After stopping the proxy, you can select an empty
data folder in Settings; existing data is copied and the old folder is retained.
Application data stays outside the app bundle, so rebuilding does not erase it.

Request and response models are counted separately. Response IDs are
deduplicated across streaming events, and dates use **Asia/Hong_Kong**.
One chat turn can produce multiple requests. A reported model name is the
captured field, not independent verification of the underlying model weights.

## Documentation and development

- [Operating guide](src/README.md): everyday controls, packet viewing and exports.
- [Storage design](STORAGE.md): archive format, retention, recovery and capture limits.
- `src/ModelScope.swift`: native SwiftUI/AppKit interface.
- `src/controller.py`: proxy lifecycle, launch settings and data management.
- `src/full_capture.py` and `src/traffic_archive.py`: capture and persistence.
- `src/packet_details.py`: per-record correlation and JSON previews.
- `tests/`: synthetic unit tests and a separate local integration check.

Run unit tests from the repository root:

```sh
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

The optional integration check starts temporary local servers and a separate
test proxy. It currently expects `mitmweb` at `/opt/homebrew/bin/mitmweb`:

```sh
python3 tests/test_proxy_capture.py
```

Git excludes installers, previous builds, backups, captures, exports, databases,
logs, certificates, private keys and local settings. The app and its tests use
synthetic fixtures for validation; no real captured conversation is included.
Legacy internal identifiers and archive headers remain for compatibility.

## License

[MIT](LICENSE). Model Monitor is an independent utility, not an official OpenAI application.
