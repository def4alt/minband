import SwiftUI

@main
struct MinBandApp: App {
    var body: some Scene {
        WindowGroup {
            ContentView()
                // Dark by design (docs/STYLE.md): no light theme.
                .preferredColorScheme(.dark)
                .tint(Theme.ink)
                .statusBarHidden()
                .persistentSystemOverlays(.hidden)
        }
    }
}
