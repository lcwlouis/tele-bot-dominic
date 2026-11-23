// swift-tools-version: 5.10
import PackageDescription

let package = Package(
    name: "DomHiggsBench",
    platforms: [.macOS("15.0")],
    dependencies: [
        .package(url: "https://github.com/ml-explore/mlx-swift", exact: "0.31.4"),
        .package(url: "https://github.com/huggingface/swift-transformers", from: "1.1.6"),
    ],
    targets: [
        .target(name: "AudioCommon", dependencies: [
            .product(name: "Hub", package: "swift-transformers"),
        ], path: "Sources/AudioCommon"),
        .target(name: "MLXCommon", dependencies: [
            "AudioCommon",
            .product(name: "MLX", package: "mlx-swift"),
            .product(name: "MLXNN", package: "mlx-swift"),
            .product(name: "MLXFast", package: "mlx-swift"),
            .product(name: "MLXFFT", package: "mlx-swift"),
        ], path: "Sources/MLXCommon"),
        .target(name: "HiggsTTS", dependencies: [
            "AudioCommon", "MLXCommon",
            .product(name: "MLX", package: "mlx-swift"),
            .product(name: "MLXNN", package: "mlx-swift"),
            .product(name: "MLXFast", package: "mlx-swift"),
            .product(name: "MLXRandom", package: "mlx-swift"),
            .product(name: "Hub", package: "swift-transformers"),
            .product(name: "Tokenizers", package: "swift-transformers"),
        ], path: "Sources/HiggsTTS"),
        .executableTarget(name: "DomHiggs", dependencies: [
            "HiggsTTS", .product(name: "MLX", package: "mlx-swift"),
        ], path: "Sources/DomHiggs"),
    ]
)
