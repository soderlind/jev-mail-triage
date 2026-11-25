// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "JevMailTriage",
    platforms: [.macOS("27.0")],
    products: [
        .executable(name: "JevMailTriage", targets: ["JevMailTriage"]),
        .library(name: "JevTriageKit", targets: ["JevTriageKit"]),
    ],
    targets: [
        .target(name: "JevTriageKit"),
        .executableTarget(name: "JevMailTriage", dependencies: ["JevTriageKit"]),
        .testTarget(
            name: "JevTriageKitTests", dependencies: ["JevTriageKit"],
            resources: [.copy("Fixtures")]),
    ]
)
