import Foundation
import Testing

/// Defaults kept in memory only: reads and writes never reach a plist,
/// the app's own domain or the global domain. A real suite would leave a
/// file in ~/Library/Preferences even after `removePersistentDomain`, so
/// tests use `TestDefaults` and never `UserDefaults(suiteName:)` directly
/// (core/tests/test_app_test_defaults.py enforces it).
///
/// Swift picks the typed `set` overloads for literals (`set(true, ...)`,
/// `set(700.0, ...)`), so each one is overridden to store into `values`;
/// otherwise it would reach the real suite. Every other read
/// (`dictionaryRepresentation`, `dictionary`, `stringArray`) and
/// `register(defaults:)` is overridden too; registered values are a
/// fallback for keys never set, as in a real suite.
final class MemoryDefaults: UserDefaults, @unchecked Sendable {
    private var values: [String: Any] = [:]
    private var registered: [String: Any] = [:]

    override func object(forKey defaultName: String) -> Any? { values[defaultName] ?? registered[defaultName] }
    override func string(forKey defaultName: String) -> String? { object(forKey: defaultName) as? String }
    override func array(forKey defaultName: String) -> [Any]? { object(forKey: defaultName) as? [Any] }
    override func stringArray(forKey defaultName: String) -> [String]? { object(forKey: defaultName) as? [String] }
    override func dictionary(forKey defaultName: String) -> [String: Any]? { object(forKey: defaultName) as? [String: Any] }
    override func data(forKey defaultName: String) -> Data? { object(forKey: defaultName) as? Data }
    override func url(forKey url: String) -> URL? { object(forKey: url) as? URL }
    override func dictionaryRepresentation() -> [String: Any] { registered.merging(values) { _, set in set } }
    override func register(defaults registrationDictionary: [String: Any]) {
        registered.merge(registrationDictionary) { _, new in new }
    }
    override func bool(forKey defaultName: String) -> Bool { number(defaultName)?.boolValue ?? false }
    override func integer(forKey defaultName: String) -> Int { number(defaultName)?.intValue ?? 0 }
    override func double(forKey defaultName: String) -> Double { number(defaultName)?.doubleValue ?? 0 }
    override func float(forKey defaultName: String) -> Float { number(defaultName)?.floatValue ?? 0 }

    override func set(_ value: Any?, forKey defaultName: String) { values[defaultName] = value }
    override func set(_ value: Bool, forKey defaultName: String) { values[defaultName] = value }
    override func set(_ value: Int, forKey defaultName: String) { values[defaultName] = value }
    override func set(_ value: Double, forKey defaultName: String) { values[defaultName] = value }
    override func set(_ value: Float, forKey defaultName: String) { values[defaultName] = value }
    override func set(_ url: URL?, forKey defaultName: String) { values[defaultName] = url }
    override func removeObject(forKey defaultName: String) { values[defaultName] = nil }

    private func number(_ key: String) -> NSNumber? { object(forKey: key) as? NSNumber }
}

/// A uniquely named in-memory defaults suite, never the app's own domain;
/// nothing to clean up.
struct TestDefaults {
    let defaults: UserDefaults = MemoryDefaults(suiteName: "agentboard-tests-\(UUID().uuidString)")!
}

@Suite("MemoryDefaults")
struct MemoryDefaultsTests {
    /// Every typed setter lands in memory and reads back, so none of them
    /// falls through to a real suite on disk.
    @Test func everyTypedSetterStoresInMemory() {
        let defaults = TestDefaults().defaults
        let url = URL(fileURLWithPath: "/tmp/a b.wav")
        defaults.set(true, forKey: "bool")
        defaults.set(7, forKey: "int")
        defaults.set(700.5, forKey: "double")
        defaults.set(Float(1.5), forKey: "float")
        defaults.set(url, forKey: "url")
        defaults.set("text", forKey: "string")
        defaults.set(["a", "b"], forKey: "array")

        #expect(defaults.object(forKey: "bool") as? Bool == true)
        #expect(defaults.object(forKey: "int") as? Int == 7)
        #expect(defaults.object(forKey: "double") as? Double == 700.5)
        #expect(defaults.object(forKey: "float") as? Float == 1.5)
        #expect(defaults.object(forKey: "url") as? URL == url)
        #expect(defaults.bool(forKey: "bool"))
        #expect(defaults.integer(forKey: "int") == 7)
        #expect(defaults.double(forKey: "double") == 700.5)
        #expect(defaults.float(forKey: "float") == 1.5)
        #expect(defaults.url(forKey: "url") == url)
        #expect(defaults.string(forKey: "string") == "text")
        #expect(defaults.array(forKey: "array") as? [String] == ["a", "b"])

        defaults.removeObject(forKey: "double")
        #expect(defaults.object(forKey: "double") == nil)
        #expect(defaults.double(forKey: "double") == 0)
    }

    /// The remaining reads and `register(defaults:)` stay in memory: a
    /// registered value is a fallback until set, and the representation
    /// holds only this instance's keys (no global domain).
    @Test func registeredValuesAndOtherReadsStayInMemory() {
        let defaults = TestDefaults().defaults
        defaults.register(defaults: ["reg": "fallback", "list": ["x"]])
        defaults.set(["k": 1], forKey: "dict")
        #expect(defaults.string(forKey: "reg") == "fallback")
        #expect(defaults.stringArray(forKey: "list") == ["x"])
        #expect(defaults.dictionary(forKey: "dict")?["k"] as? Int == 1)
        defaults.set("set", forKey: "reg")
        #expect(defaults.string(forKey: "reg") == "set")
        defaults.removeObject(forKey: "reg")
        #expect(defaults.string(forKey: "reg") == "fallback")
        #expect(Set(defaults.dictionaryRepresentation().keys) == ["reg", "list", "dict"])
    }

    /// Two instances never share values.
    @Test func instancesAreIndependent() {
        let a = TestDefaults().defaults
        let b = TestDefaults().defaults
        a.set(true, forKey: "k")
        #expect(b.object(forKey: "k") == nil)
    }
}
