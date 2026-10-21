//! The extension's description, in pure Rust.

/// The crate version, which is the package version.
pub const VERSION: &str = env!("CARGO_PKG_VERSION");

/// Whether this build holds the native dispatcher; `Machine(core="rust")` needs it.
pub const DISPATCHER: bool = true;

/// The extension's description.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CoreInfo {
    pub version: &'static str,
    pub dispatcher: bool,
    pub debug: bool,
}

pub fn core_info() -> CoreInfo {
    CoreInfo { version: VERSION, dispatcher: DISPATCHER, debug: cfg!(debug_assertions) }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn info_carries_the_crate_version() {
        let info = core_info();
        assert_eq!(info.version, env!("CARGO_PKG_VERSION"));
        assert!(!info.version.is_empty());
    }

    #[test]
    fn info_carries_the_dispatcher_flag() {
        assert_eq!(core_info().dispatcher, DISPATCHER);
    }

    #[test]
    fn info_carries_the_build_profile() {
        assert_eq!(core_info().debug, cfg!(debug_assertions));
    }
}
