import SwiftUI

/// The Help window: a topic sidebar with a search field that filters the
/// topics, and the selected topic's page with the search terms highlighted.
struct HelpView: View {
    @ObservedObject var navigator: HelpNavigator

    var body: some View {
        let search = HelpSearch(navigator.query)
        let topics = search.filter(navigator.topics)
        NavigationSplitView {
            sidebar(topics: topics, search: search)
        } detail: {
            if topics.isEmpty {
                ContentUnavailableView.search(text: navigator.query)
            } else if let topic = navigator.currentTopic, search.matches(topic) {
                HelpTopicPage(topic: topic, search: search)
            } else {
                ContentUnavailableView("Choose a topic", systemImage: "questionmark.circle",
                                       description: Text("Pick a topic from the list on the left."))
            }
        }
        .navigationTitle("AgentBoard Help")
        .onChange(of: navigator.query) { _, _ in
            // Keep a visible topic selected while the search narrows the list.
            let kept = HelpSearch(navigator.query).filter(navigator.topics)
            if let current = navigator.topic, !kept.contains(where: { $0.id == current }),
               let first = kept.first {
                navigator.topic = first.id
            }
        }
    }

    private func sidebar(topics: [HelpTopic], search: HelpSearch) -> some View {
        VStack(spacing: 0) {
            HStack(spacing: 6) {
                Image(systemName: "magnifyingglass")
                    .foregroundStyle(.secondary)
                TextField("Search help", text: $navigator.query)
                    .textFieldStyle(.plain)
                    .help("Search every help topic; matching words are highlighted")
                if !navigator.query.isEmpty {
                    Button {
                        navigator.query = ""
                    } label: {
                        Image(systemName: "xmark.circle.fill")
                    }
                    .buttonStyle(.plain)
                    .foregroundStyle(.secondary)
                    .help("Clear the help search")
                    .accessibilityLabel("Clear search")
                }
            }
            .padding(.horizontal, 8)
            .padding(.vertical, 5)
            .background(.quaternary.opacity(0.6), in: RoundedRectangle(cornerRadius: 6))
            .padding(8)

            List(selection: $navigator.topic) {
                ForEach(topics) { topic in
                    Label(topic.title, systemImage: topic.symbol)
                        .tag(Optional(topic.id))
                        .help("Show “\(topic.title)”")
                }
            }
        }
        .navigationSplitViewColumnWidth(min: 190, ideal: 220)
    }
}

/// One topic's page: its blocks in order.
struct HelpTopicPage: View {
    let topic: HelpTopic
    let search: HelpSearch

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                Label(topic.title, systemImage: topic.symbol)
                    .font(.largeTitle.bold())
                    .padding(.bottom, 4)
                ForEach(Array(topic.blocks.enumerated()), id: \.offset) { _, block in
                    HelpBlockView(block: block, search: search)
                }
            }
            .textSelection(.enabled)
            .frame(maxWidth: 680, alignment: .leading)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(24)
        }
        .id(topic.id)
    }
}

/// Renders one block of a help page.
struct HelpBlockView: View {
    let block: HelpBlock
    let search: HelpSearch

    var body: some View {
        switch block {
        case .heading(let text):
            Text(styled(text))
                .font(.title3.bold())
                .padding(.top, 8)
        case .paragraph(let text):
            Text(styled(text))
                .fixedSize(horizontal: false, vertical: true)
        case .bullets(let items):
            VStack(alignment: .leading, spacing: 6) {
                ForEach(Array(items.enumerated()), id: \.offset) { _, item in
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        Text("•").foregroundStyle(.secondary)
                        Text(styled(item))
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
        case .keyValue(let rows):
            Grid(alignment: .topLeading, horizontalSpacing: 16, verticalSpacing: 8) {
                ForEach(Array(rows.enumerated()), id: \.offset) { index, row in
                    if index > 0 { Divider() }
                    GridRow {
                        Text(styled(row.key))
                            .font(.callout.weight(.semibold))
                            .frame(minWidth: 140, maxWidth: 220, alignment: .leading)
                            .fixedSize(horizontal: false, vertical: true)
                        Text(styled(row.value))
                            .font(.callout)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
            .padding(10)
            .background(.quaternary.opacity(0.35), in: RoundedRectangle(cornerRadius: 8))
        case .shortcut(let keys, let action):
            HStack(alignment: .firstTextBaseline, spacing: 10) {
                Text(search.highlight(AttributedString(keys)))
                    .font(.callout.monospaced().bold())
                    .padding(.horizontal, 6)
                    .padding(.vertical, 2)
                    .background(.quaternary, in: RoundedRectangle(cornerRadius: 4))
                Text(styled(action))
                    .fixedSize(horizontal: false, vertical: true)
            }
        case .code(let text):
            Text(search.highlight(AttributedString(text)))
                .font(.callout.monospaced())
                .fixedSize(horizontal: false, vertical: true)
                .padding(10)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(.quaternary.opacity(0.5), in: RoundedRectangle(cornerRadius: 6))
        }
    }

    /// Inline markdown with the search terms highlighted.
    private func styled(_ markdown: String) -> AttributedString {
        let parsed = (try? HelpBlock.inlineMarkdown(markdown)) ?? AttributedString(markdown)
        return search.highlight(parsed)
    }
}
