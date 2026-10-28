import AppKit
import SwiftUI
import UniformTypeIdentifiers
import Security

struct CaptureRow: Decodable, Identifiable {
    let id: String
    let time: String
    let model: String
    let status: String
    var clock: String { String(time.dropFirst(11).prefix(12)) }
}
struct CaptureIssue: Decodable, Identifiable {
    let id: String
    let title: String
    let message: String
}
struct PacketMessage: Decodable, Identifiable {
    let id: String
    let label: String
    let direction: String
    let time: String
    let byte_count: Int64
    let text: String
    let truncated: Bool
    let path: String
    var clock: String { String(time.dropFirst(11).prefix(12)) }
}
struct PacketDetail: Decodable {
    let record: CaptureRow
    let protocol_label: String
    let url: String
    let notice: String
    let messages: [PacketMessage]
    let flow_ids: [String]
}
struct DailyReport: Decodable {
    let date: String
    let requests: [CaptureRow]
    let responses: [CaptureRow]
    let request_models: [String: Int]
    let response_models: [String: Int]
    let last_request: String?
    let updated_at: String
}
struct ProxyState: Decodable {
    let running: Bool
    let recording: Bool?
    let proxy_port: Int?
    let web_port: Int?
}
struct Settings: Codable {
    var gpt_app: String
    var mitmweb: String
    var ca_file: String
    var data_dir: String
    var proxy_port: Int
    var web_port: Int
}
struct Snapshot: Decodable {
    let proxy: ProxyState
    let settings: Settings
    let data_dir: String
    let default_data_dir: String
    let report: DailyReport
    let capture_error: String
    let capture_issues: [CaptureIssue]
    let dependencies: [String: Bool]
    let archive: ArchiveSummary
    let archive_note: String
}
struct ArchiveSummary: Decodable {
    let date: String
    let files: Int
    let bytes: Int64
}

@MainActor final class AppModel: ObservableObject {
    @Published var snapshot: Snapshot?
    @Published var date = Date()
    @Published var mode = "requests"
    @Published var modelFilter = "All models"
    @Published var busy = false
    @Published var notice = "Loading local records…"
    @Published var error: String?
    @Published var gptRunning = false
    @Published var packetRow: CaptureRow?
    @Published var packet: PacketDetail?
    @Published var packetError: String?
    @Published var activeIssue: CaptureIssue?
    @Published var showSetup = false
    private var checkedSetup = false
    private var pendingPacket: (CaptureRow, String)?
    private var packetMode = "requests"
    private var timer: Timer?
    private var launchOnReady = CommandLine.arguments.contains("--start")
    private let resource: URL
    var backendURL: URL { resource.appendingPathComponent("controller.py") }
    var dateString: String {
        let f = DateFormatter(); f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = TimeZone(identifier: "Asia/Hong_Kong"); f.dateFormat = "yyyy-MM-dd"
        return f.string(from: date)
    }
    var models: [String: Int] {
        guard let report = snapshot?.report else { return [:] }
        return mode == "requests" ? report.request_models : report.response_models
    }
    var rows: [CaptureRow] {
        guard let report = snapshot?.report else { return [] }
        let all = mode == "requests" ? report.requests : report.responses
        return modelFilter == "All models" ? all : all.filter { $0.model == modelFilter }
    }
    var total: Int { models.values.reduce(0,+) }
    var proxyRunning: Bool { snapshot?.proxy.running ?? false }
    var recording: Bool { proxyRunning && snapshot?.proxy.recording == true }
    var needsSetup: Bool {
        guard let dependencies = snapshot?.dependencies else { return false }
        return dependencies["mitmweb"] != true || dependencies["certificate"] != true || dependencies["certificate_trusted"] != true
    }
    var gptApplications: [NSRunningApplication] {
        guard let path = snapshot?.settings.gpt_app else { return [] }
        return NSWorkspace.shared.runningApplications.filter { $0.bundleURL?.path == path && !$0.isTerminated }
    }

    init() {
        resource = Bundle.main.resourceURL!
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 4, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.refresh() }
        }
    }

    func run(_ arguments: [String], input: Data? = nil, timeout: TimeInterval = 45, completion: @escaping ([String: Any]) -> Void) {
        if busy { return }
        busy = true
        let script = backendURL.path
        let helper = resource.appendingPathComponent("Helper/model-monitor-helper").path
        let hasHelper = FileManager.default.isExecutableFile(atPath: helper)
        DispatchQueue.global(qos: .userInitiated).async {
            var result: [String: Any]
            do {
                let process = Process()
                process.executableURL = URL(fileURLWithPath: hasHelper ? helper : "/usr/bin/python3")
                process.arguments = hasHelper ? arguments : [script] + arguments
                var env = ProcessInfo.processInfo.environment
                env["PYTHONDONTWRITEBYTECODE"] = "1"
                process.environment = env
                // Use a private temporary file so large daily histories cannot
                // fill an OS pipe buffer and stall the helper at shutdown.
                let outputURL = FileManager.default.temporaryDirectory.appendingPathComponent("model-trace-" + UUID().uuidString + ".json")
                FileManager.default.createFile(atPath: outputURL.path, contents: nil, attributes: [.posixPermissions: 0o600])
                let output = try FileHandle(forWritingTo: outputURL)
                defer { try? output.close(); try? FileManager.default.removeItem(at: outputURL) }
                process.standardOutput = output; process.standardError = output
                let stdin = Pipe(); process.standardInput = stdin
                let exited = DispatchSemaphore(value: 0)
                process.terminationHandler = { _ in exited.signal() }
                try process.run()
                if let input = input { stdin.fileHandleForWriting.write(input) }
                try? stdin.fileHandleForWriting.close()
                guard exited.wait(timeout: .now() + timeout) == .success else {
                    process.terminate()
                    throw NSError(domain: "ModelTrace", code: 1, userInfo: [NSLocalizedDescriptionKey: "The local helper timed out. Please refresh or check proxy.log in the data folder."])
                }
                let data = try Data(contentsOf: outputURL)
                if let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                    result = object
                } else {
                    result = ["ok": false, "error": "The local helper could not return a result. Reinstall the complete app package or check proxy.log in the data folder."]
                }
            } catch {
                result = ["ok": false, "error": error.localizedDescription]
            }
            DispatchQueue.main.async {
                self.busy = false
                if result["ok"] as? Bool == false {
                    let message = result["error"] as? String ?? "Could not complete the action"
                    if result["code"] as? String == "certificate_trust" {
                        self.activeIssue = CaptureIssue(id: UUID().uuidString, title: "Proxy certificate needs attention", message: message)
                    } else if arguments.first != "packet-detail" { self.error = message }
                }
                completion(result)
                if !self.busy, let pending = self.pendingPacket {
                    self.pendingPacket = nil
                    if self.packetRow != nil { self.loadPacket(pending.0, mode: pending.1) }
                }
            }
        }
    }

    func refresh() {
        if busy { return }
        run(["status", "--date", dateString]) { result in
            guard result["ok"] as? Bool == true,
                  let data = try? JSONSerialization.data(withJSONObject: result),
                  let status = try? JSONDecoder().decode(Snapshot.self, from: data) else { return }
            self.snapshot = status
            if !self.checkedSetup {
                self.checkedSetup = true
                if self.needsSetup { self.showSetup = true }
            }
            if self.activeIssue == nil {
                var shown = UserDefaults.standard.stringArray(forKey: "shownCaptureIssues") ?? []
                if let issue = status.capture_issues.first(where: { !shown.contains($0.id) }) {
                    self.activeIssue = issue
                    shown.append(issue.id)
                    UserDefaults.standard.set(Array(shown.suffix(100)), forKey: "shownCaptureIssues")
                }
            }
            self.gptRunning = !self.gptApplications.isEmpty
            if self.modelFilter != "All models", self.models[self.modelFilter] == nil { self.modelFilter = "All models" }
            if self.notice == "Loading local records…" { self.notice = "Records loaded. New requests are saved automatically." }
            if self.launchOnReady {
                self.launchOnReady = false
                self.startProxy(thenLaunch: true)
            }
        }
    }

    func showPacket(_ row: CaptureRow) {
        packetMode = mode; packetRow = row; packet = nil; packetError = nil
        loadPacket(row, mode: mode)
    }

    private func loadPacket(_ row: CaptureRow, mode: String) {
        if busy { pendingPacket = (row, mode); return }
        run(["packet-detail", "--mode", mode, "--id", row.id], timeout: 3600) { result in
            guard self.packetRow?.id == row.id, self.packetMode == mode else { return }
            if result["ok"] as? Bool == true,
               let data = try? JSONSerialization.data(withJSONObject: result),
               let detail = try? JSONDecoder().decode(PacketDetail.self, from: data) {
                self.packet = detail
            } else {
                self.packetError = result["error"] as? String ?? "Could not read the saved packets."
            }
        }
    }

    func closePacket() { packetRow = nil; packet = nil; packetError = nil; pendingPacket = nil }

    func reloadPacket() {
        guard let row = packetRow else { return }
        packetError = nil; loadPacket(row, mode: packetMode)
    }

    func startProxy(thenLaunch: Bool = false) {
        if needsSetup { showSetup = true; return }
        notice = "Starting proxy…"; error = nil
        run(["start"]) { result in
            guard result["ok"] as? Bool == true else { return }
            self.notice = "Proxy is running. Model records are saved automatically."
            if thenLaunch { self.launchGPT() } else { self.refresh() }
        }
    }

    func launchGPT() {
        if needsSetup { showSetup = true; return }
        error = nil
        run(["launch-plan"]) { result in
            guard result["ok"] as? Bool == true, let path = result["app"] as? String,
                  let env = result["environment"] as? [String: String], let args = result["arguments"] as? [String] else { return }
            let existing = NSWorkspace.shared.runningApplications.filter { $0.bundleURL?.path == path && !$0.isTerminated }
            if !existing.isEmpty {
                let alert = NSAlert()
                alert.messageText = "GPT is already open"
                alert.informativeText = "Restart GPT to apply the proxy settings. Finish or save any active work first."
                alert.addButton(withTitle: "Restart with proxy")
                alert.addButton(withTitle: "Show existing window")
                alert.addButton(withTitle: "Cancel")
                let choice = alert.runModal()
                if choice == .alertSecondButtonReturn {
                    existing.first?.activate(options: [.activateAllWindows, .activateIgnoringOtherApps])
                    self.notice = "Opened the existing GPT window with its current proxy settings."; self.refresh(); return
                }
                guard choice == .alertFirstButtonReturn else { self.refresh(); return }
                for app in existing { _ = app.terminate() }
                self.busy = true; self.notice = "Waiting for GPT to quit…"
                var ticks = 0
                Timer.scheduledTimer(withTimeInterval: 0.3, repeats: true) { timer in
                    Task { @MainActor in
                        ticks += 1
                        if existing.allSatisfy({ $0.isTerminated }) {
                            timer.invalidate(); self.busy = false
                            self.openGPT(path: path, environment: env, arguments: args)
                        } else if ticks >= 60 {
                            timer.invalidate(); self.busy = false
                            self.error = "GPT has not quit yet. Finish quitting GPT, then click Launch GPT."
                        }
                    }
                }
            } else { self.openGPT(path: path, environment: env, arguments: args) }
        }
    }

    func openGPT(path: String, environment: [String: String], arguments: [String]) {
        busy = true; notice = "Launching GPT…"
        let config = NSWorkspace.OpenConfiguration()
        var env = ProcessInfo.processInfo.environment
        environment.forEach { env[$0.key] = $0.value }
        config.environment = env; config.arguments = arguments; config.activates = true
        NSWorkspace.shared.openApplication(at: URL(fileURLWithPath: path), configuration: config) { app, failure in
            DispatchQueue.main.async {
                self.busy = false
                if let failure = failure { self.error = failure.localizedDescription }
                else { self.notice = "GPT launched with proxy settings. Send a message to see new records here." }
                self.refresh()
            }
        }
    }

    func stopProxy() {
        if gptRunning {
            let alert = NSAlert(); alert.messageText = "Stop the proxy?"
            alert.informativeText = "GPT connections using this proxy will disconnect. Saved records will be kept."
            alert.addButton(withTitle: "Stop proxy"); alert.addButton(withTitle: "Cancel")
            guard alert.runModal() == .alertFirstButtonReturn else { return }
        }
        run(["stop"]) { result in
            if result["ok"] as? Bool == true { self.notice = "Proxy stopped. Your records are saved."; self.refresh() }
        }
    }

    func prepareSetup() {
        error = nil; notice = "Preparing the local certificate…"
        run(["prepare-setup"], timeout: 60) { result in
            if result["ok"] as? Bool == true {
                self.notice = "Local certificate is ready. Review and approve SSL trust to continue."
            }
            self.refresh()
        }
    }

    func trustSetupCertificate() {
        guard !busy, let path = snapshot?.settings.ca_file else { return }
        let alert = NSAlert()
        alert.messageText = "Trust your local proxy certificate?"
        alert.informativeText = "This allows the proxy to decrypt HTTPS traffic sent through it. SSL trust applies to your macOS account, including other apps using its trust settings. Quitting the proxy does not revoke trust. Keep the private key and saved traffic private. macOS may ask you to authorize this change.\n\nCertificate: \(path)"
        alert.addButton(withTitle: "Trust for SSL"); alert.addButton(withTitle: "Cancel")
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        busy = true; error = nil
        DispatchQueue.global(qos: .userInitiated).async {
            var failure: String?
            do {
                let pem = try String(contentsOfFile: path, encoding: .utf8)
                let body = pem.replacingOccurrences(of: "-----BEGIN CERTIFICATE-----", with: "")
                    .replacingOccurrences(of: "-----END CERTIFICATE-----", with: "")
                    .components(separatedBy: .whitespacesAndNewlines).joined()
                guard let der = Data(base64Encoded: body), let certificate = SecCertificateCreateWithData(nil, der as CFData) else {
                    throw NSError(domain: "ModelMonitor", code: 1, userInfo: [NSLocalizedDescriptionKey: "The selected file is not a valid public CA certificate."])
                }
                let attributes: [String: Any] = [kSecClass as String: kSecClassCertificate, kSecValueRef as String: certificate]
                let imported = SecItemAdd(attributes as CFDictionary, nil)
                guard imported == errSecSuccess || imported == errSecDuplicateItem else {
                    throw NSError(domain: NSOSStatusErrorDomain, code: Int(imported))
                }
                let trust: [String: Any] = [kSecTrustSettingsPolicy as String: SecPolicyCreateSSL(true, nil),
                                           kSecTrustSettingsResult as String: NSNumber(value: SecTrustSettingsResult.trustRoot.rawValue)]
                let result = SecTrustSettingsSetTrustSettings(certificate, .user, [trust] as CFArray)
                guard result == errSecSuccess else {
                    throw NSError(domain: NSOSStatusErrorDomain, code: Int(result))
                }
            } catch { failure = error.localizedDescription }
            DispatchQueue.main.async {
                self.busy = false
                if let failure = failure {
                    self.error = "Certificate trust was not completed: \(failure). You can also set SSL trust in Keychain Access."
                } else { self.notice = "SSL trust saved for your macOS account." }
                self.refresh()
            }
        }
    }

    func openSetupCertificate() {
        guard let path = snapshot?.settings.ca_file else { return }
        NSWorkspace.shared.open(URL(fileURLWithPath: path))
    }

    func importFlows() {
        let panel = NSOpenPanel(); panel.title = "Select a flows file exported from mitmproxy"
        panel.canChooseDirectories = false; panel.allowsMultipleSelection = false
        guard panel.runModal() == .OK, let url = panel.url else { return }
        run(["import", "--path", url.path]) { result in
            if result["ok"] as? Bool == true {
                self.notice = "Imported \(result["requests"] ?? 0) requests. Duplicate records were merged."; self.refresh()
            }
        }
    }

    func export() {
        run(["export", "--date", dateString]) { result in
            if let path = result["path"] as? String {
                NSWorkspace.shared.open(URL(fileURLWithPath: path)); self.notice = "CSV files exported for the selected date."
            }
        }
    }

    func openWeb() {
        run(["web-url"]) { result in
            if let address = result["url"] as? String, let url = URL(string: address) { NSWorkspace.shared.open(url) }
        }
    }

    func openData() {
        if let path = snapshot?.data_dir { NSWorkspace.shared.open(URL(fileURLWithPath: path)) }
    }

    func openSavedTraffic() {
        notice = "Preparing saved traffic for the selected date…"; error = nil
        run(["export-traffic", "--date", dateString], timeout: 3600) { result in
            if let path = result["index"] as? String {
                NSWorkspace.shared.open(URL(fileURLWithPath: path))
                self.notice = "Saved traffic opened. Full bodies and messages are included in the export."
                if let count = result["warnings"] as? Int, count > 0 {
                    self.notice = "Saved traffic opened. Check the export notice for unfinished or interrupted records."
                }
            }
        }
    }

    func saveSettings(_ settings: Settings, done: @escaping () -> Void) {
        guard let data = try? JSONEncoder().encode(settings) else { return }
        notice = "Saving settings and copying records if needed…"; error = nil
        run(["settings"], input: data, timeout: 3600) { result in
            if result["ok"] as? Bool == true { self.notice = "Settings saved. Existing records were copied if the data folder changed."; done(); self.refresh() }
        }
    }
}

let ink = Color(red: 0.14, green: 0.23, blue: 0.32)
let accent = Color(red: 0.086, green: 0.482, blue: 0.604)

struct ContentView: View {
    @ObservedObject var model: AppModel
    @State private var showSettings = false
    @State private var selectedRowID: String?

    var body: some View {
        HStack(spacing: 0) {
            sidebar
            if model.packetRow != nil {
                PacketDetailView(model: model)
            } else {
            VStack(alignment: .leading, spacing: 18) {
                HStack {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("Daily model activity").font(.system(size: 24, weight: .semibold))
                        Text("Model counts and request timestamps").font(.system(size: 13)).foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button(action: model.export) { Label("Export CSV", systemImage: "square.and.arrow.up") }
                        .disabled(model.busy || model.snapshot == nil)
                }
                HStack(spacing: 14) {
                    DatePicker("Date", selection: $model.date, displayedComponents: .date).labelsHidden().frame(width: 130)
                        .environment(\.timeZone, TimeZone(identifier: "Asia/Hong_Kong")!)
                    Button("Today") { model.date = Date(); model.refresh() }
                    Spacer()
                    Picker("Source", selection: $model.mode) {
                        Text("Request model").tag("requests"); Text("Response model").tag("responses")
                    }.pickerStyle(.segmented).frame(width: 206)
                }
                VStack(spacing: 0) {
                    HStack { Text("Model"); Spacer(); Text("Count") }
                        .font(.system(size: 12, weight: .medium)).foregroundStyle(.secondary).padding(.bottom, 10)
                    Divider()
                    if model.models.isEmpty {
                        Text("No model records for this date").foregroundStyle(.secondary).frame(maxWidth: .infinity, alignment: .leading).padding(.vertical, 18)
                    } else {
                        ScrollView {
                            VStack(spacing: 0) {
                                ForEach(model.models.keys.sorted(), id: \.self) { name in
                                    HStack { Text(name).textSelection(.enabled); Spacer(); Text("\(model.models[name] ?? 0)").monospacedDigit().fontWeight(.semibold) }
                                        .padding(.vertical, 9)
                                    Divider().opacity(0.5)
                                }
                            }
                        }.frame(height: CGFloat(min(max(model.models.count, 1) * 39, 126)))
                    }
                }.padding(16).background(Color.white).clipShape(RoundedRectangle(cornerRadius: 8))
                HStack {
                    Text(model.mode == "requests" ? "Request timestamps" : "First response timestamps").font(.system(size: 15, weight: .semibold))
                    Text("\(model.total) records").font(.system(size: 12)).foregroundStyle(.secondary)
                    Spacer()
                    Picker("Filter model", selection: $model.modelFilter) {
                        Text("All models").tag("All models")
                        ForEach(model.models.keys.sorted(), id: \.self) { Text($0).tag($0) }
                    }.labelsHidden().frame(maxWidth: 225)
                }
                if model.rows.isEmpty {
                    VStack(spacing: 14) {
                        Image(systemName: "tray").font(.system(size: 32)).foregroundStyle(accent)
                        Text("Waiting for new requests").font(.system(size: 17, weight: .medium))
                        Text("Click Start Proxy & GPT, then send a message in GPT.\nYou can also import a saved flows file.")
                            .multilineTextAlignment(.center).font(.system(size: 13)).foregroundStyle(.secondary)
                        Button("Import flows…", action: model.importFlows).disabled(model.busy)
                    }.frame(maxWidth: .infinity, maxHeight: .infinity)
                } else {
                    Table(model.rows, selection: $selectedRowID) {
                        TableColumn("Time (Hong Kong)") { row in Text(row.clock).monospacedDigit() }.width(min: 140, ideal: 165, max: 200)
                        TableColumn("Model") { row in Text(row.model) }
                        TableColumn("") { _ in Image(systemName: "chevron.right").foregroundStyle(.secondary) }.width(18)
                    }.tableStyle(.inset(alternatesRowBackgrounds: true))
                        .clipShape(RoundedRectangle(cornerRadius: 6))
                        .help("Click a record to view its saved request and response")
                }
                VStack(alignment: .leading, spacing: 5) {
                    HStack {
                        if model.busy { ProgressView().controlSize(.small) }
                        Text(model.notice).font(.system(size: 12)).foregroundStyle(.secondary)
                        Spacer()
                        Button { model.refresh() } label: { Image(systemName: "arrow.clockwise") }.help("Refresh records").disabled(model.busy)
                    }
                    if let error = model.error {
                        HStack(alignment: .top) {
                            Text(error).font(.system(size: 12)).foregroundStyle(Color(red: 0.68, green: 0.18, blue: 0.16)).textSelection(.enabled)
                            Spacer(); Button { model.error = nil } label: { Image(systemName: "xmark") }.buttonStyle(.plain)
                        }
                    }
                    if let warning = model.snapshot?.capture_error, !warning.isEmpty { Text(warning).font(.caption).foregroundColor(.red) }
                    if let snapshot = model.snapshot {
                        Text(snapshot.archive_note).font(.caption).foregroundStyle(.secondary)
                        Text("Saved traffic for this date: \(ByteCountFormatter.string(fromByteCount: snapshot.archive.bytes, countStyle: .file)) · \(snapshot.archive.files) archive files")
                            .font(.system(size: 11)).foregroundStyle(.secondary)
                    }
                    Text("Captured traffic only. Dates use Hong Kong time. Streaming events are deduplicated.")
                        .font(.system(size: 11)).foregroundStyle(.secondary)
                }
            }.padding(26).background(Color(red: 0.965, green: 0.973, blue: 0.98))
            }
        }
        .frame(minWidth: 930, minHeight: 660)
        .tint(accent)
        .onChange(of: model.date) { _ in model.refresh() }
        .onChange(of: model.mode) { _ in model.modelFilter = "All models" }
        .onChange(of: selectedRowID) { id in
            if let row = model.rows.first(where: { $0.id == id }) {
                model.showPacket(row)
                selectedRowID = nil
            }
        }
        .alert(item: $model.activeIssue) { issue in
            Alert(title: Text(issue.title), message: Text(issue.message), dismissButton: .default(Text("OK")))
        }
        .sheet(isPresented: $showSettings) {
            if let settings = model.snapshot?.settings { SettingsView(model: model, settings: settings) }
        }
        .sheet(isPresented: $model.showSetup) { CaptureSetupView(model: model) }
    }

    var sidebar: some View {
        VStack(alignment: .leading, spacing: 22) {
            HStack(spacing: 10) {
                Image(systemName: "point.3.connected.trianglepath.dotted").font(.system(size: 28))
                VStack(alignment: .leading, spacing: 3) {
                    Text("Model\nMonitor").font(.system(size: 19, weight: .semibold)).fixedSize(horizontal: false, vertical: true)
                    Text("Local proxy & model history").font(.system(size: 11)).opacity(0.65)
                }
            }.padding(.bottom, 14)
            VStack(alignment: .leading, spacing: 10) {
                statusLine(model.proxyRunning ? (model.recording ? "Proxy is recording" : "Recording is not active") : "Proxy is stopped", active: model.recording)
                Text(model.proxyRunning ? "127.0.0.1:\(String(model.snapshot?.proxy.proxy_port ?? 18741))" : "Default port \(String(model.snapshot?.settings.proxy_port ?? 18741))")
                    .font(.system(size: 12)).opacity(0.65).padding(.leading, 17)
                statusLine(model.gptRunning ? "GPT is running" : "GPT is not running", active: model.gptRunning)
            }
            VStack(spacing: 10) {
                Button { model.startProxy(thenLaunch: true) } label: {
                    HStack(spacing: 8) {
                        Image(systemName: "play.fill")
                        Text("Start Proxy & GPT")
                    }.font(.system(size: 13, weight: .semibold)).foregroundStyle(ink)
                        .frame(maxWidth: .infinity).padding(.vertical, 12)
                        .background(Color(red: 0.83, green: 0.92, blue: 0.96))
                        .clipShape(RoundedRectangle(cornerRadius: 7))
                }.buttonStyle(.plain)
                    .disabled(model.busy)
                VStack(spacing: 8) {
                    Button("Start proxy") { model.startProxy() }.disabled(model.busy || model.proxyRunning)
                    Button("Launch GPT", action: model.launchGPT).disabled(model.busy)
                }.controlSize(.large)
                Button("Stop proxy", action: model.stopProxy).disabled(model.busy || !model.proxyRunning).frame(maxWidth: .infinity)
            }
            Text("Records save automatically.\nThe proxy keeps running when this window closes.")
                .font(.system(size: 12)).lineSpacing(5).opacity(0.65)
            Divider().overlay(Color.white.opacity(0.2))
            VStack(alignment: .leading, spacing: 16) {
                railButton("Import saved flows", icon: "square.and.arrow.down", action: model.importFlows)
                railButton("Open capture viewer", icon: "network", action: model.openWeb)
                railButton("Open saved traffic", icon: "archivebox", action: model.openSavedTraffic)
                railButton("Open data folder", icon: "folder", action: model.openData)
            }
            Spacer()
            railButton(model.needsSetup ? "Finish setup" : "Capture setup", icon: "checkmark.shield") { model.showSetup = true }
            railButton("Settings", icon: "gearshape") { showSettings = true }
            Text("Model summaries and retained packets are saved locally. Full captures may include credentials.")
                .font(.system(size: 11)).lineSpacing(4).opacity(0.55)
        }.padding(24).frame(width: 234).frame(maxHeight: .infinity).background(ink).foregroundStyle(.white)
    }

    func statusLine(_ label: String, active: Bool) -> some View {
        HStack(spacing: 8) {
            Circle().fill(active ? Color(red: 0.44, green: 0.82, blue: 0.64) : Color.white.opacity(0.35)).frame(width: 8, height: 8)
            Text(label).font(.system(size: 13, weight: .medium))
        }
    }
    func railButton(_ name: String, icon: String, action: @escaping () -> Void) -> some View {
        Button(action: action) { Label(name, systemImage: icon).font(.system(size: 13)).frame(maxWidth: .infinity, alignment: .leading) }
            .buttonStyle(.plain).disabled(model.busy)
    }
}

struct PacketDetailView: View {
    @ObservedObject var model: AppModel
    @State private var side = "response"
    @State private var selectedMessageID: String?

    var messages: [PacketMessage] { model.packet?.messages.filter { $0.direction == side } ?? [] }
    var selected: PacketMessage? { messages.first { $0.id == selectedMessageID } ?? messages.first }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack {
                Button(action: model.closePacket) { Label("Model activity", systemImage: "chevron.left") }
                Spacer()
                Button(action: model.reloadPacket) { Label("Refresh packets", systemImage: "arrow.clockwise") }
                    .disabled(model.busy)
            }
            if let row = model.packet?.record ?? model.packetRow {
                VStack(alignment: .leading, spacing: 6) {
                    Text(row.model).font(.system(size: 24, weight: .semibold)).textSelection(.enabled)
                    Text(row.time.replacingOccurrences(of: "T", with: " ") + "  ·  " + row.status)
                        .font(.system(size: 12)).foregroundStyle(.secondary).textSelection(.enabled)
                    Text(row.id).font(.system(size: 11, design: .monospaced)).foregroundStyle(.secondary).textSelection(.enabled)
                }
            }
            if let detail = model.packet {
                if let error = model.packetError { Text(error).font(.system(size: 12)).foregroundStyle(.red).textSelection(.enabled) }
                HStack(alignment: .top) {
                    Text(detail.protocol_label).font(.system(size: 12, weight: .semibold))
                    if !detail.url.isEmpty { Text(detail.url).font(.system(size: 12)).foregroundStyle(.secondary).textSelection(.enabled) }
                }
                if !detail.notice.isEmpty {
                    Text(detail.notice).font(.system(size: 12)).textSelection(.enabled)
                        .padding(12).frame(maxWidth: .infinity, alignment: .leading)
                        .background(Color.orange.opacity(0.09)).clipShape(RoundedRectangle(cornerRadius: 6))
                }
                Picker("Packet direction", selection: $side) {
                    ForEach(["request", "response", "connection"], id: \.self) { value in
                        Text("\(value.capitalized) (\(detail.messages.filter { $0.direction == value }.count))").tag(value)
                    }
                }.pickerStyle(.segmented)
                HStack(spacing: 0) {
                    ScrollView {
                        LazyVStack(spacing: 4) {
                            ForEach(messages) { message in
                                Button { selectedMessageID = message.id } label: {
                                    VStack(alignment: .leading, spacing: 5) {
                                        Text(message.label).font(.system(size: 12, weight: .medium)).lineLimit(3)
                                        Text(message.clock + " · " + ByteCountFormatter.string(fromByteCount: message.byte_count, countStyle: .file))
                                            .font(.system(size: 10)).foregroundStyle(.secondary)
                                    }.frame(maxWidth: .infinity, alignment: .leading).padding(10)
                                        .background(selected?.id == message.id ? accent.opacity(0.12) : Color.clear)
                                        .clipShape(RoundedRectangle(cornerRadius: 5)).contentShape(Rectangle())
                                }.buttonStyle(.plain)
                            }
                        }.padding(8)
                    }.frame(width: 210)
                    Divider()
                    if let message = selected {
                        VStack(alignment: .leading, spacing: 10) {
                            HStack {
                                Text(message.label).font(.system(size: 12, weight: .semibold))
                                Spacer()
                                Button { NSPasteboard.general.clearContents(); NSPasteboard.general.setString(message.text, forType: .string) } label: {
                                    Image(systemName: "doc.on.doc")
                                }.help("Copy shown text")
                                Button {
                                    NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: message.path)])
                                } label: { Image(systemName: "folder") }.help("Reveal full file in Finder")
                            }
                            if message.truncated {
                                Text("Preview shortened. The full captured content is available in the file.")
                                    .font(.system(size: 11)).foregroundStyle(.secondary)
                            }
                            PacketTextView(text: message.text.isEmpty ? "(Empty content)" : message.text, messageID: message.id)
                                .frame(maxWidth: .infinity, maxHeight: .infinity)
                        }.padding(14).frame(maxWidth: .infinity, maxHeight: .infinity)
                    } else {
                        Text("No linked \(side) content was saved for this record.")
                            .font(.system(size: 13)).foregroundStyle(.secondary)
                            .frame(maxWidth: .infinity, maxHeight: .infinity).padding(20)
                    }
                }.background(Color.white).clipShape(RoundedRectangle(cornerRadius: 8))
                Text("Saved content only. Opening this page does not send or replay requests.")
                    .font(.system(size: 11)).foregroundStyle(.secondary)
            } else if let error = model.packetError {
                Text(error).foregroundStyle(.red).textSelection(.enabled)
                Spacer()
            } else {
                VStack(spacing: 14) {
                    ProgressView()
                    Text("Reading saved packets…").foregroundStyle(.secondary)
                }.frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }.padding(24).frame(maxWidth: .infinity, maxHeight: .infinity)
            .background(Color(red: 0.965, green: 0.973, blue: 0.98))
            .onAppear { side = model.mode == "requests" ? "request" : "response" }
            .onChange(of: side) { _ in selectedMessageID = nil }
    }
}

struct PacketTextView: NSViewRepresentable {
    let text: String
    let messageID: String

    final class Coordinator { var messageID: String? }
    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeNSView(context: Context) -> NSScrollView {
        let scroll = NSScrollView()
        scroll.hasVerticalScroller = true
        scroll.hasHorizontalScroller = false
        scroll.autohidesScrollers = true
        scroll.borderType = .noBorder
        let content = NSTextView(frame: .zero)
        content.isEditable = false
        content.isSelectable = true
        content.isRichText = false
        content.font = .monospacedSystemFont(ofSize: 12, weight: .regular)
        content.textColor = .textColor
        content.backgroundColor = .white
        content.textContainerInset = NSSize(width: 6, height: 6)
        content.minSize = .zero
        content.maxSize = NSSize(width: CGFloat.greatestFiniteMagnitude, height: CGFloat.greatestFiniteMagnitude)
        content.isVerticallyResizable = true
        content.isHorizontallyResizable = false
        content.autoresizingMask = [.width]
        content.textContainer?.widthTracksTextView = true
        content.textContainer?.containerSize = NSSize(width: scroll.contentSize.width, height: CGFloat.greatestFiniteMagnitude)
        content.layoutManager?.allowsNonContiguousLayout = true
        scroll.documentView = content
        return scroll
    }

    func updateNSView(_ scroll: NSScrollView, context: Context) {
        guard let content = scroll.documentView as? NSTextView else { return }
        if content.string != text || context.coordinator.messageID != messageID {
            content.string = text
            content.setSelectedRange(NSRange(location: 0, length: 0))
            content.scrollToBeginningOfDocument(nil)
            context.coordinator.messageID = messageID
        }
    }
}

struct CaptureSetupView: View {
    @ObservedObject var model: AppModel
    @Environment(\.dismiss) var dismiss
    var proxyReady: Bool { model.snapshot?.dependencies["mitmweb"] == true }
    var certificateReady: Bool { model.snapshot?.dependencies["certificate"] == true }
    var trusted: Bool { model.snapshot?.dependencies["certificate_trusted"] == true }

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            Text("Set up capture").font(.system(size: 25, weight: .semibold))
            Text("Prepare your Mac once, then start capturing from the main window.")
                .foregroundStyle(.secondary)
            step("Proxy runtime", ready: proxyReady,
                 detail: model.snapshot?.dependencies["bundled_proxy"] == true ? "Included in this app. No Homebrew installation needed." : "Uses the mitmweb installation selected in Settings.")
            step("Local certificate", ready: certificateReady,
                 detail: "Created on your Mac. Your private key stays in your data folder; an existing selected CA is reused.")
            step("SSL trust", ready: trusted,
                 detail: "Requires your approval. Trust applies to your macOS account and may affect other apps using its trust settings.")
            if let path = model.snapshot?.settings.ca_file {
                Text(path).font(.system(size: 11, design: .monospaced)).foregroundStyle(.secondary).textSelection(.enabled)
            }
            if !certificateReady {
                Button("Prepare Local Certificate", action: model.prepareSetup)
                    .buttonStyle(.borderedProminent).disabled(model.busy || !proxyReady || model.proxyRunning)
            } else if !trusted {
                HStack {
                    Button("Trust Certificate…", action: model.trustSetupCertificate).buttonStyle(.borderedProminent)
                    Button("Open in Keychain", action: model.openSetupCertificate)
                }.disabled(model.busy)
            }
            if !proxyReady { Text("Install the complete package or select mitmweb in Settings.").foregroundStyle(.orange) }
            if model.proxyRunning && !certificateReady { Text("Stop the proxy before preparing a certificate.").foregroundStyle(.orange) }
            if let error = model.error { Text(error).font(.callout).foregroundStyle(.red).textSelection(.enabled) }
            Text("Captured requests and responses can include credentials. Closing the app keeps the proxy running. Removing SSL trust later is a separate action in Keychain Access.")
                .font(.footnote).foregroundStyle(.secondary)
            HStack {
                if model.busy { ProgressView().controlSize(.small) }
                Spacer()
                Button("Check Again") { model.refresh() }.disabled(model.busy)
                Button(trusted && proxyReady ? "Done" : "Later") { dismiss() }.disabled(model.busy)
            }
        }.padding(28).frame(width: 590)
    }

    func step(_ title: String, ready: Bool, detail: String) -> some View {
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: ready ? "checkmark.circle.fill" : "circle")
                .foregroundColor(ready ? .green : .secondary).font(.title3)
            VStack(alignment: .leading, spacing: 4) {
                Text(title).font(.headline)
                Text(detail).font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
        }
    }
}

struct SettingsView: View {
    @ObservedObject var model: AppModel
    @State var settings: Settings
    @Environment(\.dismiss) var dismiss

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            Text("Launch settings").font(.system(size: 22, weight: .semibold))
            Text("Proxy settings apply to GPT launched here. System proxy settings stay unchanged.")
                .font(.system(size: 12)).foregroundStyle(.secondary)
            pathField("GPT application", value: $settings.gpt_app, directory: true)
            pathField("mitmweb", value: $settings.mitmweb)
            pathField("Proxy certificate", value: $settings.ca_file)
            VStack(alignment: .leading, spacing: 7) {
                pathField("Data folder", value: $settings.data_dir, directory: true)
                HStack {
                    Button("Use standard macOS folder") { settings.data_dir = model.snapshot?.default_data_dir ?? settings.data_dir }
                    Spacer()
                }
                Text("Stored outside the app. Choose an empty folder; existing records are copied when you save. Stop the proxy before changing it. The old folder stays as a backup.")
                    .font(.system(size: 12)).foregroundStyle(.secondary)
            }
            HStack(spacing: 20) {
                Text("Proxy port")
                TextField("18741", value: $settings.proxy_port, formatter: NumberFormatter()).frame(width: 85)
                Text("Capture viewer port")
                TextField("18742", value: $settings.web_port, formatter: NumberFormatter()).frame(width: 85)
            }.font(.system(size: 13))
            Text("If a port is busy, an available port is selected. Stop this proxy before changing settings.")
                .font(.system(size: 12)).foregroundStyle(.secondary)
            if let error = model.error { Text(error).font(.system(size: 12)).foregroundColor(.red) }
            HStack {
                Spacer(); Button("Cancel") { dismiss() }
                Button("Save") { model.saveSettings(settings) { dismiss() } }
                    .buttonStyle(.borderedProminent).disabled(model.busy || model.proxyRunning)
            }
        }.padding(28).frame(width: 700)
    }
    func pathField(_ name: String, value: Binding<String>, directory: Bool = false) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(name).font(.system(size: 12, weight: .medium))
            HStack {
                TextField(name, text: value).textFieldStyle(.roundedBorder)
                Button("Choose…") {
                    let panel = NSOpenPanel(); panel.title = "Choose \(name)"
                    panel.canChooseDirectories = directory; panel.canChooseFiles = !directory || name == "GPT application"
                    panel.treatsFilePackagesAsDirectories = false
                    if panel.runModal() == .OK, let url = panel.url { value.wrappedValue = url.path }
                }
            }
        }
    }
}

@MainActor final class AppDelegate: NSObject, NSApplicationDelegate {
    var window: NSWindow!
    var model: AppModel!
    func applicationDidFinishLaunching(_ notification: Notification) {
        model = AppModel()
        window = NSWindow(contentRect: NSRect(x: 0,y: 0,width: 1060,height: 760), styleMask: [.titled,.closable,.miniaturizable,.resizable], backing: .buffered, defer: false)
        window.title = "Model Monitor"
        window.contentView = NSHostingView(rootView: ContentView(model: model).environment(\.colorScheme, .light).environment(\.locale, Locale(identifier: "en_US")))
        window.minSize = NSSize(width: 930,height: 680)
        window.center(); window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        let menu = NSMenu()
        let appItem = NSMenuItem(); menu.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "About Model Monitor", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        appMenu.addItem(NSMenuItem.separator())
        appMenu.addItem(withTitle: "Quit Model Monitor", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        let editItem = NSMenuItem(); menu.addItem(editItem)
        let edit = NSMenu(title: "Edit")
        edit.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = edit; NSApp.mainMenu = menu
    }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        window.makeKeyAndOrderFront(nil); return true
    }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
}

@main struct Main {
    @MainActor static func main() {
        let app = NSApplication.shared
        app.setActivationPolicy(.regular)
        let delegate = AppDelegate()
        app.delegate = delegate
        app.run()
    }
}
