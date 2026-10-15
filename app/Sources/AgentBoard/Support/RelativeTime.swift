import Foundation

enum RelativeTime {
    static func string(from isoString: String, relativeTo now: Date = Date()) -> String {
        guard let date = ISO8601DateParsing.date(from: isoString) else { return isoString }
        let formatter = RelativeDateTimeFormatter()
        formatter.unitsStyle = .abbreviated
        return formatter.localizedString(for: date, relativeTo: now)
    }
}
