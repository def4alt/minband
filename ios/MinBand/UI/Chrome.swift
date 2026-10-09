import SwiftUI

/// 1 pt rule in `ink3`, horizontal or vertical.
struct Hairline: View {
    enum Axis { case horizontal, vertical }
    var axis: Axis = .horizontal
    var color: Color = Theme.ink3

    var body: some View {
        switch axis {
        case .horizontal: Rectangle().fill(color).frame(height: Theme.hairline)
        case .vertical: Rectangle().fill(color).frame(width: Theme.hairline)
        }
    }
}
