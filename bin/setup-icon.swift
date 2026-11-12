import AppKit

let arguments = Array(CommandLine.arguments.dropFirst())
if arguments == ["refresh"] {
    NSUpdateDynamicServices()
    exit(0)
}
guard arguments.count == 2,
      let image = NSImage(contentsOfFile: arguments[0]),
      NSWorkspace.shared.setIcon(image, forFile: arguments[1], options: []) else {
    exit(1)
}
