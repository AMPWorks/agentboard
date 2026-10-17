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
        ),
        // Swift Testing (`import Testing`), not XCTest: the Command Line
        // Tools ship Testing.framework but no XCTest.framework. With only the
        // CLT installed, SwiftPM does not put that framework on the search or
        // runtime paths; see README "Testing" for the `swift test` flags.
        .testTarget(
            name: "AgentBoardTests",
            dependencies: ["AgentBoard"],
            swiftSettings: [
                .swiftLanguageMode(.v6)
            ]
        ),
    ]
)
