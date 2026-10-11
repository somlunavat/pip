// Pip Listener: listens for "Hey Pip" on-device and tells the overlay.
//
// Runs as its own tiny app bundle (bin/PipListener.app, no Dock icon) so macOS asks
// for Microphone and Speech Recognition for it, with the usage strings in its
// Info.plist. Recognition is on-device only (requiresOnDeviceRecognition): audio is
// never sent anywhere, and the listener refuses to run if on-device recognition
// isn't available.
//
// Talks to the overlay over a Unix socket (--socket PATH), one line per event:
//   ready                 listening for the wake phrase
//   wake                  heard "hey pip"; the overlay shows Listening…
//   command <text>        what the user said after the wake phrase
//   cancel                heard the wake phrase but nothing after it
//   error <message>       can't listen (permission, no on-device model, ...)
// The overlay sends "quit" to stop it.

import AVFoundation
import Cocoa
import Speech

let wakePattern = try! NSRegularExpression(
    pattern: #"\b(hey|hi|ok|okay|yo|hello)[\s,.!]+(pip|pipp|pips|pep|peep|pippy|pit)\b"#,
    options: [.caseInsensitive])

let commandSilence: TimeInterval = 1.2  // stop collecting after this much quiet
let commandWait: TimeInterval = 6.0     // give up if nothing follows the wake phrase
let sessionLength: TimeInterval = 50    // restart before Apple's ~1 min task limit

// MARK: - Socket to the overlay

final class Link {
    private var fd: Int32 = -1

    init?(path: String) {
        fd = socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else { return nil }
        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)
        let bytes = Array(path.utf8.prefix(MemoryLayout.size(ofValue: addr.sun_path) - 1))
        withUnsafeMutableBytes(of: &addr.sun_path) { raw in
            for (i, b) in bytes.enumerated() { raw[i] = b }
        }
        let size = socklen_t(MemoryLayout<sockaddr_un>.size)
        let ok = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { connect(fd, $0, size) }
        }
        guard ok == 0 else { close(fd); return nil }
    }

    func send(_ line: String) {
        let data = Array((line.replacingOccurrences(of: "\n", with: " ") + "\n").utf8)
        _ = data.withUnsafeBufferPointer { write(fd, $0.baseAddress, $0.count) }
    }

    /// Calls onQuit when the overlay says quit or goes away.
    func watch(onQuit: @escaping () -> Void) {
        DispatchQueue.global(qos: .utility).async { [fd] in
            var buf = [UInt8](repeating: 0, count: 256)
            while true {
                let n = read(fd, &buf, buf.count)
                if n <= 0 { break }
                if String(decoding: buf[0..<n], as: UTF8.self).contains("quit") { break }
            }
            DispatchQueue.main.async(execute: onQuit)
        }
    }
}

// MARK: - Listener

final class Listener {
    private let link: Link
    private let recognizer = SFSpeechRecognizer(locale: Locale(identifier: "en-US"))!
    private let engine = AVAudioEngine()
    private var request: SFSpeechAudioBufferRecognitionRequest?
    private var task: SFSpeechRecognitionTask?
    private var sessionStart = Date()

    // Wake state
    private var awake = false
    private var wakeAt = Date()
    private var command = ""
    private var lastChange = Date()
    private var cooldownUntil = Date.distantPast

    init(link: Link) { self.link = link }

    func start() {
        guard recognizer.supportsOnDeviceRecognition else {
            fail("on-device speech recognition isn't available on this Mac")
            return
        }
        SFSpeechRecognizer.requestAuthorization { status in
            DispatchQueue.main.async {
                guard status == .authorized else {
                    self.fail("Speech Recognition is off for Pip Listener. Allow it in System "
                              + "Settings > Privacy & Security > Speech Recognition.")
                    return
                }
                AVCaptureDevice.requestAccess(for: .audio) { granted in
                    DispatchQueue.main.async {
                        guard granted else {
                            self.fail("Microphone is off for Pip Listener. Allow it in System "
                                      + "Settings > Privacy & Security > Microphone.")
                            return
                        }
                        self.startAudio()
                    }
                }
            }
        }
    }

    private func fail(_ message: String) {
        link.send("error \(message)")
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { exit(1) }
    }

    private func startAudio() {
        let input = engine.inputNode
        let format = input.outputFormat(forBus: 0)
        input.installTap(onBus: 0, bufferSize: 1024, format: format) { [weak self] buffer, _ in
            self?.request?.append(buffer)
        }
        do {
            engine.prepare()
            try engine.start()
        } catch {
            fail("couldn't start the microphone: \(error.localizedDescription)")
            return
        }
        newSession()
        Timer.scheduledTimer(withTimeInterval: 0.2, repeats: true) { [weak self] _ in self?.tick() }
        link.send("ready")
    }

    private func newSession() {
        task?.cancel()
        request?.endAudio()
        let req = SFSpeechAudioBufferRecognitionRequest()
        req.requiresOnDeviceRecognition = true  // never leaves the Mac
        req.shouldReportPartialResults = true
        req.contextualStrings = ["Hey Pip", "Pip"]
        request = req
        sessionStart = Date()
        task = recognizer.recognitionTask(with: req) { [weak self] result, error in
            guard let self else { return }
            if let result {
                self.heard(result.bestTranscription.formattedString)
                if result.isFinal { self.finish() }
            } else if error != nil {
                // Sessions end on long silence or errors; just start a fresh one.
                DispatchQueue.main.async { self.restart() }
            }
        }
    }

    private func restart() {
        awake = false
        command = ""
        newSession()
    }

    private func heard(_ transcript: String) {
        // Everything after the last wake phrase is the request.
        guard let text = parseWake(transcript) else { return }
        if !awake {
            guard Date() >= cooldownUntil else { return }
            awake = true
            wakeAt = Date()
            lastChange = Date()
            command = text
            link.send("wake")
        } else if text != command {
            command = text
            lastChange = Date()
        }
    }

    private func finish() {
        guard awake else { return }
        if command.isEmpty {
            link.send("cancel")
        } else {
            link.send("command \(command)")
        }
        cooldownUntil = Date().addingTimeInterval(1.0)
        DispatchQueue.main.async { self.restart() }
    }

    private func tick() {
        let now = Date()
        if awake {
            if !command.isEmpty && now.timeIntervalSince(lastChange) > commandSilence {
                finish()
            } else if command.isEmpty && now.timeIntervalSince(wakeAt) > commandWait {
                finish()
            }
        } else if now.timeIntervalSince(sessionStart) > sessionLength {
            restart()
        }
    }
}

// MARK: - Main

/// The request after the last wake phrase, or nil if there is no wake phrase.
func parseWake(_ transcript: String) -> String? {
    let range = NSRange(transcript.startIndex..., in: transcript)
    guard let match = wakePattern.matches(in: transcript, range: range).last,
          let end = Range(match.range, in: transcript)?.upperBound else { return nil }
    return String(transcript[end...])
        .trimmingCharacters(in: CharacterSet.whitespacesAndNewlines.union(.punctuationCharacters))
}

let args = CommandLine.arguments

// Self-check without a microphone: PipListener --parse "hey pip what's the weather" ...
if args.count > 2, args[1] == "--parse" {
    for phrase in args.dropFirst(2) {
        if let command = parseWake(phrase) {
            print("wake  \(phrase)  ->  \(command.isEmpty ? "(nothing yet)" : command)")
        } else {
            print("none  \(phrase)")
        }
    }
    exit(0)
}
guard let i = args.firstIndex(of: "--socket"), i + 1 < args.count,
      let link = Link(path: args[i + 1]) else {
    FileHandle.standardError.write("usage: PipListener --socket PATH (overlay must be running)\n"
        .data(using: .utf8)!)
    exit(2)
}

let app = NSApplication.shared
app.setActivationPolicy(.prohibited)
let listener = Listener(link: link)
link.watch { exit(0) }
listener.start()
app.run()
