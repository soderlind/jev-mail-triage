import Foundation
import LocalAuthentication
import Security

/// Checks whether helper-owned Keychain items exist, without reading their secret data.
/// The app never reads or stores secrets; it hands new values to the helper over stdin.
public enum KeychainPresence {
    public static let service = "jev-mail-triage"
    public static let typeSafeAccount = "typesafe-api-key"

    public static func bridgeAccount(_ accountID: String) -> String { "bridge:\(accountID)" }

    public static func exists(account: String, service: String = KeychainPresence.service) -> Bool {
        let context = LAContext()
        context.interactionNotAllowed = true
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecMatchLimit as String: kSecMatchLimitOne,
            kSecReturnAttributes as String: true,
            kSecUseAuthenticationContext as String: context,
        ]
        var item: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &item)
        // A locked keychain still proves the item exists.
        return status == errSecSuccess || status == errSecInteractionNotAllowed
    }
}
