// clickwatch: reports double-clicks that happen in other apps, so the overlay can
// close when the user double-clicks outside it.
//
// Uses a passive NSEvent global monitor: it only observes mouse-down events sent
// to other apps (it cannot see keystrokes, and cannot block or change clicks), and
// mouse monitoring needs no extra macOS permission.
//
// Output, one line per double-click, in Electron screen coordinates (points,
// origin at the top-left of the primary display):
//     dbl <x> <y>
// Exits when stdin closes, i.e. when the overlay process goes away.

import Cocoa

setvbuf(stdout, nil, _IOLBF, 0)

let app = NSApplication.shared
app.setActivationPolicy(.prohibited)

let monitor = NSEvent.addGlobalMonitorForEvents(matching: [.leftMouseDown, .rightMouseDown]) { event in
    guard event.clickCount == 2 else { return }
    let point = NSEvent.mouseLocation
    let primaryHeight = NSScreen.screens.first?.frame.height ?? 0
    print("dbl \(Int(point.x)) \(Int(primaryHeight - point.y))")
}

if monitor == nil {
    FileHandle.standardError.write("clickwatch: could not start the mouse monitor\n".data(using: .utf8)!)
    exit(1)
}

DispatchQueue.global(qos: .utility).async {
    while readLine() != nil {}
    exit(0)
}

app.run()
