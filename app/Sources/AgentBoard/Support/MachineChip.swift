import Foundation

/// The short label on a Mac's chip (a rounded square, against the round
/// C/X tool badge): its initial, or two letters, or initial plus a digit
/// when two Macs share an initial. Derived from every Mac in id order, so a
/// Mac keeps its label from snapshot to snapshot and in every view.
enum MachineChip {
    /// The name's first letter or digit, uppercased; "?" when it has none.
    static func initial(_ name: String) -> String {
        guard let first = name.first(where: { $0.isLetter || $0.isNumber }) else { return "?" }
        return String(first).uppercased()
    }

    /// Chip label per machine id.
    static func labels(for machines: [Machine]) -> [String: String] {
        var byID: [String: String] = [:]
        for machine in machines where byID[machine.id] == nil { byID[machine.id] = machine.name }
        let ids = byID.keys.sorted()
        let groups = Dictionary(grouping: ids) { initial(byID[$0]!) }
        var labels: [String: String] = [:]
        for (letter, members) in groups where members.count == 1 { labels[members[0]] = letter }
        for (letter, members) in groups.sorted(by: { $0.key < $1.key }) where members.count > 1 {
            let two = members.map { twoLetters(byID[$0]!) }
            let taken = Set(labels.values)
            if two.allSatisfy({ $0 != nil }), Set(two.compactMap { $0 }).count == members.count,
               taken.isDisjoint(with: two.compactMap { $0 }) {
                for (id, label) in zip(members, two) { labels[id] = label }
            } else {
                for (index, id) in members.enumerated() { labels[id] = "\(letter)\(index + 1)" }
            }
        }
        return labels
    }

    /// The initial plus the name's next letter or digit ("Mi" for mini).
    private static func twoLetters(_ name: String) -> String? {
        let chars = name.filter { $0.isLetter || $0.isNumber }
        guard chars.count >= 2 else { return nil }
        return String(chars.first!).uppercased() + String(chars.dropFirst().first!).lowercased()
    }
}

extension Snapshot {
    /// Chip labels for every Mac the snapshot names (paired Macs and the
    /// Macs of its sessions).
    var machineChipLabels: [String: String] {
        MachineChip.labels(for: peers.map { Machine(id: $0.id, name: $0.name) } + sessions.compactMap(\.machine))
    }

    func chipLabel(for machine: Machine) -> String {
        machineChipLabels[machine.id] ?? MachineChip.initial(machine.name)
    }
}
