// swift-tools-version:6.0
import PackageDescription

let package = Package(
    name: "AgentBoard",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(
            name: "AgentBoard",
            swiftSettings: [
                .swiftLanguageMode(.v6)
            ]
        )
    ]
)
