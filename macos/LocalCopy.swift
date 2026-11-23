import Darwin
import Foundation
import Security

enum LocalHexKey {
    static func generate(bytes: Int) throws -> String {
        guard (1...64).contains(bytes) else { throw LocalCopy.Failure("Неверная длина ключа.") }
        var buffer = [UInt8](repeating: 0, count: bytes)
        guard SecRandomCopyBytes(kSecRandomDefault, bytes, &buffer) == errSecSuccess else {
            throw LocalCopy.Failure("macOS не удалось сгенерировать случайный ключ.")
        }
        return buffer.map { String(format: "%02X", $0) }.joined()
    }

    static func decode(_ text: String, bytes: Int) throws -> [UInt8] {
        let digits = Array(text.utf8)
        guard digits.count == bytes * 2, digits.allSatisfy({ (48...57).contains($0) || (65...70).contains($0) || (97...102).contains($0) }) else {
            throw LocalCopy.Failure(LF("Введите ровно %ld шестнадцатеричных символов (0–9, A–F).", bytes * 2))
        }
        return stride(from: 0, to: digits.count, by: 2).map {
            UInt8(String(decoding: digits[$0..<$0 + 2], as: UTF8.self), radix: 16)!
        }
    }
}

/// File operations for the local service editors. A published copy always has a new name.
enum LocalCopy {
    struct Failure: LocalizedError {
        let message: String
        init(_ message: String) { self.message = message }
        var errorDescription: String? { L(message) }
    }

    static func read(_ url: URL, maximumSize: Int) throws -> Data {
        let scoped = url.startAccessingSecurityScopedResource()
        defer { if scoped { url.stopAccessingSecurityScopedResource() } }
        let fd = open(url.path, O_RDONLY | O_CLOEXEC | O_NONBLOCK)
        guard fd >= 0 else { throw POSIXError(.init(rawValue: errno) ?? .EIO) }
        let handle = FileHandle(fileDescriptor: fd, closeOnDealloc: true)
        defer { try? handle.close() }
        var info = stat()
        guard fstat(fd, &info) == 0, (info.st_mode & S_IFMT) == S_IFREG,
              info.st_size <= maximumSize else {
            throw Failure("Выберите обычный файл подходящего размера.")
        }
        var contents = Data()
        while contents.count <= maximumSize {
            let count = min(65536, maximumSize + 1 - contents.count)
            guard let part = try handle.read(upToCount: count), !part.isEmpty else {
                guard contents.count == info.st_size else { throw Failure("Размер файла изменился при чтении.") }
                return contents
            }
            contents.append(part)
        }
        throw Failure("Размер файла превышает ожидаемый.")
    }

    static func write(_ data: Data, to url: URL) throws {
        let scoped = url.startAccessingSecurityScopedResource()
        defer { if scoped { url.stopAccessingSecurityScopedResource() } }
        let directory = url.deletingLastPathComponent()
        var pattern = Array(directory.appendingPathComponent(".tyt-copy.XXXXXX").path.utf8CString)
        let fd = mkstemp(&pattern) // Creates a new file with mode 0600 on POSIX volumes.
        guard fd >= 0 else { throw POSIXError(.init(rawValue: errno) ?? .EIO) }
        defer { pattern.withUnsafeBufferPointer { _ = unlink($0.baseAddress!) } }
        let handle = FileHandle(fileDescriptor: fd, closeOnDealloc: true)
        try handle.write(contentsOf: data)
        try handle.synchronize()
        try handle.close()
        let result = pattern.withUnsafeBufferPointer { renamex_np($0.baseAddress!, url.path, UInt32(RENAME_EXCL)) }
        guard result == 0 else {
            if errno == EEXIST { throw Failure("Файл с таким именем уже существует. Выберите новое имя для копии.") }
            throw POSIXError(.init(rawValue: errno) ?? .EIO)
        }
    }
}
