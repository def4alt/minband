//! uniffi bindgen CLI built from this crate, so the generator always matches the scaffolding
//! version compiled into the library. Host-only; used by `tools/build-ios.sh`:
//!
//! ```text
//! cargo run --features ios --bin uniffi-bindgen -- generate \
//!   --library target/aarch64-apple-ios/release/libminband_core.a --language swift --out-dir ...
//! ```

#[cfg(not(target_os = "ios"))]
fn main() {
    uniffi::uniffi_bindgen_main()
}

/// The CLI feature of uniffi is only enabled for host targets (see Cargo.toml), so an iOS build of
/// every target (`cargo build --target aarch64-apple-ios --features ios`) still compiles.
#[cfg(target_os = "ios")]
fn main() {
    eprintln!("uniffi-bindgen is a host tool; run it with `cargo run --features ios --bin uniffi-bindgen`");
    std::process::exit(2);
}
