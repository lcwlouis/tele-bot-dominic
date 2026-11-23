import Darwin
import Foundation
import HiggsTTS
import MLX

struct Request: Decodable {
    let text: String
    let seed: UInt64
    let output: String
}

let protocolOutput = FileHandle(fileDescriptor: dup(STDOUT_FILENO), closeOnDealloc: true)

func emit(_ fields: [String: Any]) throws {
    let data = try JSONSerialization.data(withJSONObject: fields, options: [.sortedKeys])
    protocolOutput.write(data + Data([10]))
}

func writeWAV(_ samples: [Float], sampleRate: Int, to url: URL) throws {
    var wave = Data()
    func ascii(_ string: String) { wave.append(contentsOf: string.utf8) }
    func integer<T: FixedWidthInteger>(_ value: T) {
        withUnsafeBytes(of: value.littleEndian) { wave.append(contentsOf: $0) }
    }
    let size = UInt32(samples.count * 2)
    ascii("RIFF")
    integer(size + 36)
    ascii("WAVEfmt ")
    integer(UInt32(16))
    integer(UInt16(1))
    integer(UInt16(1))
    integer(UInt32(sampleRate))
    integer(UInt32(sampleRate * 2))
    integer(UInt16(2))
    integer(UInt16(16))
    ascii("data")
    integer(size)
    for sample in samples {
        integer(Int16(clamping: Int((max(-1, min(1, sample)) * 32768).rounded())))
    }
    try wave.write(to: url, options: .atomic)
}

@main
struct DomHiggs {
    static func main() async throws {
        _ = protocolOutput
        dup2(STDERR_FILENO, STDOUT_FILENO)
        let arguments = CommandLine.arguments
        guard arguments.count == 4 else {
            FileHandle.standardError.write(Data("usage: DomHiggs BUNDLE WAV TRANSCRIPT_FILE\n".utf8))
            exit(2)
        }
        let bundle = URL(fileURLWithPath: arguments[1])
        let audio = URL(fileURLWithPath: arguments[2])
        let transcript = try String(contentsOfFile: arguments[3], encoding: .utf8)
            .trimmingCharacters(in: .newlines)
        let start = ProcessInfo.processInfo.systemUptime
        let model = try await HiggsTTSModel.fromBundle(bundle)
        try model.prepareRuntime()
        let loaded = ProcessInfo.processInfo.systemUptime
        let reference = try model.encodeReference(audio: audio, text: transcript)
        let referenceReady = ProcessInfo.processInfo.systemUptime
        Memory.clearCache()
        try emit(["event": "ready", "seconds": loaded - start, "reference_seconds": referenceReady - loaded,
                  "ready_seconds": ProcessInfo.processInfo.systemUptime - start, "pid": getpid()])
        while let row = readLine() {
            do {
                let request = try JSONDecoder().decode(Request.self, from: Data(row.utf8))
                let options = try HiggsTTSSynthesisOptions(temperature: 1.0, seed: request.seed)
                let before = ProcessInfo.processInfo.systemUptime
                let samples = try model.generate(text: request.text, references: [reference], options: options)
                let seconds = ProcessInfo.processInfo.systemUptime - before
                guard samples.allSatisfy(\.isFinite), samples.contains(where: { $0 != 0 }) else {
                    throw CocoaError(.fileWriteUnknown)
                }
                let output = URL(fileURLWithPath: request.output)
                try writeWAV(samples, sampleRate: model.sampleRate, to: output)
                Memory.clearCache()
                try emit(["event": "done", "seconds": seconds])
            } catch {
                try emit(["event": "error"])
            }
        }
    }
}
