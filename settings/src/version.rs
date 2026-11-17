//! Whether this binary is a release or a source build.

/// What the release and Flatpak builds set `VINOWHISPER_VERSION` to (the tag without its `v`).
const STAMP: Option<&str> = option_env!("VINOWHISPER_VERSION");

/// The release version, or `dev` for any build without the stamp, so the binary says what it
/// is wherever it was installed from.
pub const VERSION: &str = version_of(STAMP);

// A tag that disagrees with Cargo.toml must not compile.
const _: () = assert!(
    STAMP.is_none() || same(VERSION, env!("CARGO_PKG_VERSION")),
    "VINOWHISPER_VERSION must equal the Cargo.toml version"
);

const fn version_of(stamp: Option<&str>) -> &str {
    match stamp {
        Some(v) => v,
        None => "dev",
    }
}

const fn same(a: &str, b: &str) -> bool {
    let (a, b) = (a.as_bytes(), b.as_bytes());
    if a.len() != b.len() {
        return false;
    }
    let mut i = 0;
    while i < a.len() {
        if a[i] != b[i] {
            return false;
        }
        i += 1;
    }
    true
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_release_build_reports_its_version() {
        assert_eq!(version_of(Some("1.8.0")), "1.8.0");
    }

    #[test]
    fn an_unstamped_build_reports_dev() {
        assert_eq!(version_of(None), "dev");
    }

    #[test]
    fn this_build_is_dev_or_the_package_version() {
        assert!(VERSION == "dev" || VERSION == env!("CARGO_PKG_VERSION"));
    }

    #[test]
    fn a_stamp_must_equal_the_package_version() {
        assert!(same("1.8.0", "1.8.0"));
        assert!(!same("1.8.1", "1.8.0"));
        assert!(!same("1.8", "1.8.0"));
    }
}
