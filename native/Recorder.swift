import Foundation
import AVFoundation
import CoreAudio
import AudioToolbox

func emit(_ value: [String: Any]) {
    if let data = try? JSONSerialization.data(withJSONObject: value), let s = String(data: data, encoding: .utf8) { print(s); fflush(stdout) }
}
func devices() -> [[String: Any]] {
    var address = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyDevices, mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &size) == noErr else { return [] }
    var ids = [AudioDeviceID](repeating: 0, count: Int(size) / MemoryLayout<AudioDeviceID>.size)
    guard AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &size, &ids) == noErr else { return [] }
    return ids.compactMap { id in
        var streams = AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyStreams, mScope: kAudioDevicePropertyScopeInput, mElement: kAudioObjectPropertyElementMain)
        var n: UInt32 = 0
        guard AudioObjectGetPropertyDataSize(id, &streams, 0, nil, &n) == noErr, n > 0 else { return nil }
        var name: CFString = "" as CFString
        var nameSize = UInt32(MemoryLayout<CFString>.size)
        var prop = AudioObjectPropertyAddress(mSelector: kAudioObjectPropertyName, mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
        guard AudioObjectGetPropertyData(id, &prop, 0, nil, &nameSize, &name) == noErr else { return nil }
        return ["id": Int(id), "name": name as String]
    }
}
if CommandLine.arguments.contains("--devices") { emit(["devices": devices()]); exit(0) }
guard CommandLine.arguments.count >= 2 else { emit(["error":"Missing output path"]); exit(1) }
let semaphore = DispatchSemaphore(value: 0)
var allowed = false
switch AVCaptureDevice.authorizationStatus(for: .audio) {
case .authorized: allowed = true
case .notDetermined:
    AVCaptureDevice.requestAccess(for: .audio) { result in allowed = result; semaphore.signal() }
    // TCC may dispatch its completion on the main run loop. Blocking that
    // thread with semaphore.wait() can prevent the permission dialog completing.
    while semaphore.wait(timeout: .now()) == .timedOut {
        _ = RunLoop.current.run(mode: .default, before: Date(timeIntervalSinceNow: 0.1))
    }
default: break
}
guard allowed else { emit(["error":"Нет доступа к микрофону. Разрешите доступ в настройках macOS."]); exit(1) }
let engine = AVAudioEngine()
let input = engine.inputNode
if CommandLine.arguments.count > 2, let id = UInt32(CommandLine.arguments[2]), id != 0, let unit = input.audioUnit {
    var device = AudioDeviceID(id)
    let status = AudioUnitSetProperty(unit, kAudioOutputUnitProperty_CurrentDevice, kAudioUnitScope_Global, 0, &device, UInt32(MemoryLayout<AudioDeviceID>.size))
    if status != noErr { emit(["error":"Не удалось выбрать микрофон (\(status))."]); exit(1) }
}
let format = input.outputFormat(forBus: 0)
guard format.sampleRate > 0 && format.channelCount > 0 else { emit(["error":"Микрофон недоступен."]); exit(1) }
// Audio capture is isolated from the transport so another source (such as
// system audio) can use the same library/import and transcription pipeline.
final class Events {
    private let condition = NSCondition()
    private var pending: [String] = []
    func send(_ event: String) {
        condition.lock(); pending.append(event); condition.signal(); condition.unlock()
    }
    func next() -> String {
        condition.lock(); defer { condition.unlock() }
        while pending.isEmpty { condition.wait() }
        return pending.removeFirst()
    }
}
let events = Events()
var exitStatus: Int32 = 0
do {
    let file = try AVAudioFile(forWriting: URL(fileURLWithPath: CommandLine.arguments[1]), settings: format.settings)
    let meterQueue = DispatchQueue(label: "com.localscribe.meter")
    let meterLock = NSLock()
    var meterPending = false
    let errorLock = NSLock()
    var writeFailed = false
    input.installTap(onBus: 0, bufferSize: 4096, format: format) { buffer, _ in
        do { try file.write(from: buffer) } catch {
            errorLock.lock()
            if !writeFailed { writeFailed = true; events.send("error:" + error.localizedDescription) }
            errorLock.unlock()
            return
        }
        if let channels = buffer.floatChannelData {
            let count = Int(buffer.frameLength)
            if count > 0 {
                var sum: Float = 0
                for i in 0..<count { sum += channels[0][i] * channels[0][i] }
                let level = min(1.0, Double(sqrt(sum / Float(count))) * 5)
                meterLock.lock(); let shouldSend = !meterPending
                if shouldSend { meterPending = true }; meterLock.unlock()
                if shouldSend {
                    meterQueue.async {
                        emit(["level": level])
                        meterLock.lock(); meterPending = false; meterLock.unlock()
                    }
                }
            }
        }
    }
    try engine.start()
    let observer = NotificationCenter.default.addObserver(forName: .AVAudioEngineConfigurationChange, object: engine, queue: nil) { _ in
        events.send("error:Конфигурация аудиоустройства изменилась. Запись остановлена; сохранённый фрагмент доступен в библиотеке.")
    }
    Thread.detachNewThread {
        while let command = readLine() { events.send(command) }
        events.send("stop") // The controlling service disappeared.
    }
    emit(["state":"recording"])
    var running = true
    while running {
        let command = events.next()
        if command.hasPrefix("error:") {
            emit(["error": String(command.dropFirst(6))]); exitStatus = 1; running = false
        } else if command == "pause" {
            engine.pause(); emit(["state":"paused"])
        } else if command == "resume" {
            do { try engine.start(); emit(["state":"recording"]) }
            catch { emit(["error":error.localizedDescription]); exitStatus = 1; running = false }
        } else if command == "stop" { running = false }
    }
    NotificationCenter.default.removeObserver(observer)
    engine.stop(); input.removeTap(onBus: 0)
    meterQueue.sync {}
    emit(["state":"stopped"])
} catch { emit(["error":error.localizedDescription]); exitStatus = 1 }
exit(exitStatus)
