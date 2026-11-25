import Foundation

/// Version of the JSON protocol spoken by the bundled helper (`jev-mail-triage rpc`).
public let helperProtocolVersion = 1

/// Arbitrary JSON, used for request parameters and loosely typed response fields.
public enum JSONValue: Codable, Equatable, Sendable {
    case null
    case bool(Bool)
    case number(Double)
    case string(String)
    case array([JSONValue])
    case object([String: JSONValue])

    public init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() {
            self = .null
        } else if let value = try? container.decode(Bool.self) {
            self = .bool(value)
        } else if let value = try? container.decode(Double.self) {
            self = .number(value)
        } else if let value = try? container.decode(String.self) {
            self = .string(value)
        } else if let value = try? container.decode([JSONValue].self) {
            self = .array(value)
        } else {
            self = .object(try container.decode([String: JSONValue].self))
        }
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .null: try container.encodeNil()
        case .bool(let value): try container.encode(value)
        case .number(let value):
            if value.rounded() == value, abs(value) < 1e15 {
                try container.encode(Int64(value))
            } else {
                try container.encode(value)
            }
        case .string(let value): try container.encode(value)
        case .array(let value): try container.encode(value)
        case .object(let value): try container.encode(value)
        }
    }

    public var stringValue: String? {
        if case .string(let value) = self { return value }
        return nil
    }

    public var displayText: String {
        switch self {
        case .null: return "—"
        case .bool(let value): return value ? "yes" : "no"
        case .number(let value):
            return value.rounded() == value ? String(Int64(value)) : String(value)
        case .string(let value): return value
        case .array(let values): return values.map(\.displayText).joined(separator: ", ")
        case .object: return "{…}"
        }
    }
}

extension JSONValue: ExpressibleByStringLiteral, ExpressibleByBooleanLiteral,
    ExpressibleByIntegerLiteral, ExpressibleByFloatLiteral, ExpressibleByDictionaryLiteral,
    ExpressibleByArrayLiteral, ExpressibleByNilLiteral
{
    public init(stringLiteral value: String) { self = .string(value) }
    public init(booleanLiteral value: Bool) { self = .bool(value) }
    public init(integerLiteral value: Int) { self = .number(Double(value)) }
    public init(floatLiteral value: Double) { self = .number(value) }
    public init(dictionaryLiteral elements: (String, JSONValue)...) {
        self = .object(Dictionary(elements, uniquingKeysWith: { _, last in last }))
    }
    public init(arrayLiteral elements: JSONValue...) { self = .array(elements) }
    public init(nilLiteral: ()) { self = .null }

    public static func strings(_ values: [String]) -> JSONValue { .array(values.map { .string($0) }) }
    public static func optional(_ value: String?) -> JSONValue { value.map { .string($0) } ?? .null }
}

/// Structured error returned by the helper (`ok: false`).
public struct HelperError: Error, Decodable, Equatable, Sendable, LocalizedError {
    public let code: String
    public let message: String
    public let retryable: Bool

    public init(code: String, message: String, retryable: Bool) {
        self.code = code
        self.message = message
        self.retryable = retryable
    }

    public var errorDescription: String? { message }

    /// A short, actionable hint for the most common setup failures.
    public var recoverySuggestion: String? {
        switch code {
        case "not_configured": return "Open Setup to connect Proton Mail Bridge."
        case "bridge_unavailable": return "Start Proton Mail Bridge and make sure it is signed in."
        case "bridge_auth": return "Copy the Bridge password again from Proton Mail Bridge → Mailbox details."
        case "tls_verification": return "Export Bridge's certificate and select it under Bridge settings."
        case "secret_missing": return "Enter the missing secret in Setup."
        case "keychain_denied": return "Allow Jev Mail Triage Helper to use the Keychain item when macOS asks."
        case "typesafe_auth": return "Check that the TypeSafe API key was copied completely."
        case "typesafe_unavailable": return "Check the network connection and try again."
        case "busy": return "Another triage process holds the lock. Stop the other worker first."
        case "confirmation_required": return "Confirm that messages may be moved."
        default: return nil
        }
    }
}

struct ResponseEnvelope<Result: Decodable>: Decodable {
    let `protocol`: Int
    let id: String?
    let ok: Bool
    let result: Result?
    let error: HelperError?
}

struct ProtocolHeader: Decodable {
    let `protocol`: Int
}

/// Decodes one helper response line, enforcing protocol version and request id.
public enum ResponseDecoder {
    public static let jsonDecoder: JSONDecoder = {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return decoder
    }()

    public static func decode<Result: Decodable>(
        _ data: Data, expectingID id: String?, as _: Result.Type
    ) throws -> Result {
        let trimmed = data.trimmingTrailingWhitespace()
        guard !trimmed.isEmpty else { throw HelperClientError.malformedResponse("empty output") }
        guard !trimmed.contains(UInt8(ascii: "\n")) else {
            throw HelperClientError.malformedResponse("more than one line of output")
        }
        let header: ProtocolHeader
        do {
            header = try jsonDecoder.decode(ProtocolHeader.self, from: trimmed)
        } catch {
            throw HelperClientError.malformedResponse("not a protocol response")
        }
        guard header.protocol == helperProtocolVersion else {
            throw HelperClientError.protocolMismatch(helper: header.protocol, app: helperProtocolVersion)
        }
        let envelope: ResponseEnvelope<Result>
        do {
            envelope = try jsonDecoder.decode(ResponseEnvelope<Result>.self, from: trimmed)
        } catch {
            throw HelperClientError.malformedResponse("unexpected result shape: \(error)")
        }
        if let id, envelope.id != id {
            throw HelperClientError.malformedResponse("response id does not match request")
        }
        if envelope.ok {
            guard let result = envelope.result else {
                throw HelperClientError.malformedResponse("ok response without result")
            }
            return result
        }
        guard let error = envelope.error else {
            throw HelperClientError.malformedResponse("error response without error")
        }
        throw error
    }
}

extension Data {
    func trimmingTrailingWhitespace() -> Data {
        var end = endIndex
        while end > startIndex, [0x20, 0x0A, 0x0D, 0x09].contains(self[index(before: end)]) {
            end = index(before: end)
        }
        var start = startIndex
        while start < end, [0x20, 0x0A, 0x0D, 0x09].contains(self[start]) {
            start = index(after: start)
        }
        return self[start..<end]
    }
}
