import SwiftUI
import Testing
@testable import AgentBoard

@Suite("Help content")
struct HelpContentTests {
    @Test func everyTopicIdHasExactlyOnePage() {
        let ids = HelpContent.topics.map(\.id)
        #expect(Set(ids).count == ids.count)
        #expect(Set(ids) == Set(HelpTopic.ID.allCases))
        #expect(HelpContent.topics.first?.id == .gettingStarted)
    }

    @Test func everyTopicHasContent() {
        for topic in HelpContent.topics {
            #expect(!topic.title.isEmpty)
            #expect(!topic.blocks.isEmpty, "\(topic.id) has no blocks")
        }
    }

    @Test func everySettingsKeyIsDocumentedInTheSettingsTopic() throws {
        let page = try #require(HelpContent.topic(.settings))
        for key in SettingsKeys.all {
            #expect(page.searchText.contains("`\(key)`"), "Settings help does not name the key \(key)")
        }
    }

    /// `HelpBlockView` falls back to the raw text when markdown does not
    /// parse, and the parser renders unbalanced syntax literally, so check
    /// both: every rendered string parses, and no `**` or backtick survives.
    @Test func everyHelpStringIsValidInlineMarkdown() {
        for topic in HelpContent.topics {
            for block in topic.blocks {
                for text in block.markdownText {
                    #expect(throws: Never.self, "\(topic.id): \(text)") {
                        let rendered = String(try HelpBlock.inlineMarkdown(text).characters)
                        #expect(!rendered.contains("**"), "\(topic.id): stray ** in \(text)")
                        #expect(!rendered.contains("`"), "\(topic.id): stray backtick in \(text)")
                    }
                }
            }
        }
    }

    @Test func settingsKeysAreUnique() {
        #expect(Set(SettingsKeys.all).count == SettingsKeys.all.count)
    }
}

@Suite("Help search")
struct HelpSearchTests {
    private func ids(_ query: String) -> [HelpTopic.ID] {
        HelpSearch(query).filter(HelpContent.topics).map(\.id)
    }

    @Test func emptyQueryListsEveryTopic() {
        #expect(ids("") == HelpContent.topics.map(\.id))
        #expect(ids("   ") == HelpContent.topics.map(\.id))
        #expect(HelpSearch(" ").isEmpty)
    }

    @Test func matchesATopicByTitle() {
        #expect(ids("dashboard").contains(.dashboard))
        #expect(ids("CLI REFERENCE").contains(.cli))
        #expect(!ids("CLI REFERENCE").contains(.popover))
    }

    @Test func matchesATopicByBodyText() {
        // Only in tables and paragraphs, never in a title.
        #expect(ids("retitle_max_per_run").contains(.config))
        #expect(ids("menuBarIconFlashStop") == [.settings])
        #expect(ids("⌃⌥⌘A").contains(.gettingStarted))
    }

    @Test func everyTermMustMatch() {
        #expect(ids("automation_logins retitle_command").contains(.config))
        #expect(ids("automation_logins zzzz-not-in-help").isEmpty)
    }

    @Test func ignoresCaseAndAccents() {
        #expect(HelpSearch("RÉVIEW").matches(HelpContent.sessionStates))
    }

    @Test func highlightMarksEveryOccurrence() {
        let text = AttributedString("Review the review queue")
        let highlighted = HelpSearch("review").highlight(text)
        let marked = highlighted.runs.filter { $0.backgroundColor != nil }.count
        #expect(marked == 2)
        #expect(String(highlighted.characters) == "Review the review queue")
    }

    @Test func emptyQueryHighlightsNothing() {
        let text = AttributedString("Review")
        #expect(HelpSearch("").highlight(text) == text)
    }
}

@Suite("Help navigator")
@MainActor
struct HelpNavigatorTests {
    @Test func selectsTheRequestedTopic() {
        let navigator = HelpNavigator()
        navigator.select(.settings)
        #expect(navigator.topic == .settings)
        #expect(navigator.currentTopic?.id == .settings)
        navigator.select(rawID: "updates")
        #expect(navigator.topic == .updates)
    }

    @Test func unknownIdFallsBackToGettingStarted() {
        let navigator = HelpNavigator()
        navigator.select(.cli)
        navigator.select(rawID: "no-such-topic")
        #expect(navigator.topic == .gettingStarted)
    }

    @Test func aTopicMissingFromTheGuideFallsBackToGettingStarted() {
        let navigator = HelpNavigator(topics: [HelpContent.gettingStarted, HelpContent.cli])
        navigator.select(.settings)
        #expect(navigator.topic == .gettingStarted)
    }

    @Test func selectingClearsASearchThatWouldHideTheTopic() {
        let navigator = HelpNavigator()
        navigator.query = "menuBarIconFlashStop"
        navigator.select(.cli)
        #expect(navigator.query.isEmpty)
        #expect(navigator.topic == .cli)
    }

    @Test func selectingKeepsASearchThatShowsTheTopic() {
        let navigator = HelpNavigator()
        navigator.query = "menuBarIconFlashStop"
        navigator.select(.settings)
        #expect(navigator.query == "menuBarIconFlashStop")
    }
}
