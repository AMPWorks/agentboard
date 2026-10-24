import Foundation
import SwiftUI

/// One page of the in-app guide. The content is Swift data
/// (`HelpContent.topics`), not bundled files, so it needs no resource
/// bundling, is searchable in-process and is checked by tests.
struct HelpTopic: Identifiable, Sendable, Equatable {
    enum ID: String, CaseIterable, Sendable {
        case gettingStarted = "getting-started"
        case sessionStates = "session-states"
        case popover
        case updates
        case search
        case dashboard
        case links
        case settings
        case config
        case cli
        case diagnostics
        case privacy
    }

    let id: ID
    let title: String
    /// SF Symbol shown in the sidebar.
    let symbol: String
    let blocks: [HelpBlock]

    /// Every word a search can match: the title and all block text.
    var searchText: String {
        ([title] + blocks.flatMap(\.searchText)).joined(separator: "\n")
    }
}

/// A row of a key/value table (a setting, a config.json key, a command).
struct HelpRow: Sendable, Equatable {
    /// Plain text or inline markdown, usually a `code` name.
    let key: String
    /// Inline markdown.
    let value: String
}

enum HelpBlock: Sendable, Equatable {
    case heading(String)
    /// Inline markdown (bold, italics, `code`, links).
    case paragraph(String)
    /// Each item inline markdown.
    case bullets([String])
    /// A table of names and what they do.
    case keyValue([HelpRow])
    /// A keyboard shortcut or gesture and what it does.
    case shortcut(keys: String, action: String)
    /// Preformatted text: a command line or a config.json example.
    case code(String)

    var searchText: [String] {
        switch self {
        case .heading(let text), .paragraph(let text), .code(let text):
            return [text]
        case .bullets(let items):
            return items
        case .keyValue(let rows):
            return rows.flatMap { [$0.key, $0.value] }
        case .shortcut(let keys, let action):
            return [keys, action]
        }
    }

    /// The strings the Help window renders as inline markdown (see
    /// `HelpBlock.inlineMarkdown`); shortcut keys and code are plain text.
    var markdownText: [String] {
        switch self {
        case .heading(let text), .paragraph(let text):
            return [text]
        case .bullets(let items):
            return items
        case .keyValue(let rows):
            return rows.flatMap { [$0.key, $0.value] }
        case .shortcut(_, let action):
            return [action]
        case .code:
            return []
        }
    }

    /// Parses one help string the way the Help window renders it.
    static func inlineMarkdown(_ markdown: String) throws -> AttributedString {
        try AttributedString(
            markdown: markdown, options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace)
        )
    }
}

/// The Help window's search: whitespace-separated terms, every one of which
/// must appear (case- and diacritic-insensitive) in a topic's title or text.
struct HelpSearch: Sendable, Equatable {
    let terms: [String]

    init(_ query: String) {
        terms = query.split(whereSeparator: \.isWhitespace).map { Self.fold(String($0)) }
    }

    var isEmpty: Bool { terms.isEmpty }

    func matches(_ topic: HelpTopic) -> Bool {
        guard !terms.isEmpty else { return true }
        let haystack = Self.fold(topic.searchText)
        return terms.allSatisfy { haystack.contains($0) }
    }

    /// The topics this query keeps, in guide order; every topic for an
    /// empty query.
    func filter(_ topics: [HelpTopic]) -> [HelpTopic] {
        topics.filter(matches)
    }

    static func fold(_ text: String) -> String {
        text.folding(options: [.caseInsensitive, .diacriticInsensitive], locale: nil)
    }

    /// `text` with every occurrence of a term given a highlight background.
    func highlight(_ text: AttributedString) -> AttributedString {
        guard !terms.isEmpty else { return text }
        var result = text
        for term in terms {
            var searchStart = result.startIndex
            while searchStart < result.endIndex,
                  let range = result[searchStart...].range(
                      of: term, options: [.caseInsensitive, .diacriticInsensitive]) {
                result[range].backgroundColor = .yellow.opacity(0.45)
                guard range.upperBound > searchStart else { break }
                searchStart = range.upperBound
            }
        }
        return result
    }
}

/// The Help window's current topic and search query. Shared, because a
/// `Window(id:)` scene takes no value: `HelpWindowController.show(topic:)`
/// sets the topic here and the sidebar's selection is bound to it.
@MainActor
final class HelpNavigator: ObservableObject {
    static let shared = HelpNavigator()

    @Published var topic: HelpTopic.ID? = .gettingStarted
    @Published var query: String = ""

    let topics: [HelpTopic]

    init(topics: [HelpTopic] = HelpContent.topics) {
        self.topics = topics
    }

    /// Shows `id`, clearing a search that would hide it from the sidebar.
    /// A topic the guide does not have falls back to Getting started.
    func select(_ id: HelpTopic.ID) {
        let target = topics.contains { $0.id == id } ? id : .gettingStarted
        if !HelpSearch(query).isEmpty,
           let page = topics.first(where: { $0.id == target }), !HelpSearch(query).matches(page) {
            query = ""
        }
        topic = target
    }

    /// `select` by raw id (e.g. "settings"); an unknown id lands on
    /// Getting started.
    func select(rawID: String) {
        select(HelpTopic.ID(rawValue: rawID) ?? .gettingStarted)
    }

    var currentTopic: HelpTopic? {
        topics.first { $0.id == topic } ?? topics.first
    }
}
