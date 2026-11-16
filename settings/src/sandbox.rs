//! What differs inside the Flatpak, picked at run time so one build serves both.

use std::ffi::OsString;
use std::path::{Path, PathBuf};
use std::sync::OnceLock;

use configparser::ini::Ini;

/// The Flatpak's app id when this runs inside its sandbox; None natively.
pub fn flatpak_id() -> Option<&'static str> {
    static ID: OnceLock<Option<String>> = OnceLock::new();
    ID.get_or_init(|| {
        read_id(
            Path::new("/.flatpak-info"),
            std::env::var("FLATPAK_ID").ok(),
        )
    })
    .as_deref()
}

// FLATPAK_ID alone can be left in the environment of something started from a sandbox (pinned
// by `tests::the_app_id_comes_from_the_info_file_only_inside_a_sandbox`).
fn read_id(info: &Path, env: Option<String>) -> Option<String> {
    let text = std::fs::read_to_string(info).ok()?;
    if let Some(id) = env.filter(|id| !id.is_empty()) {
        return Some(id);
    }
    ini_value(&text, "Application", "name")
}

/// `key` of `[section]` in `/.flatpak-info`; None natively.
pub fn info_value(section: &str, key: &str) -> Option<String> {
    ini_value(
        &std::fs::read_to_string("/.flatpak-info").ok()?,
        section,
        key,
    )
}

// A GKeyFile: `=` only, keys as written, `#` comments on their own lines, and a `;` or `#`
// inside a value is part of it (pinned by `tests::flatpak_lists_keep_their_semicolons_and_hashes`).
fn ini_value(text: &str, section: &str, key: &str) -> Option<String> {
    let mut options = Ini::new_cs().defaults();
    options.delimiters = vec!['='];
    options.comment_symbols = vec!['#'];
    options.enable_inline_comments = false;
    let mut file = Ini::new_from_defaults(options);
    file.read(text.to_owned()).ok()?;
    file.get(section, key).filter(|value| !value.is_empty())
}

/// The branch of a release's bundle; a build from a checkout is `master`.
pub const RELEASE_BRANCH: &str = "stable";

/// Whether this is a release's Flatpak bundle.
pub fn is_release() -> bool {
    flatpak_id().is_some() && info_value("Instance", "branch").as_deref() == Some(RELEASE_BRANCH)
}

/// The runtime directory every sandbox of the app shares: `app/<id>` in the Flatpak.
pub fn runtime_dir() -> Option<PathBuf> {
    runtime_dir_for(std::env::var_os("XDG_RUNTIME_DIR"), flatpak_id())
}

fn runtime_dir_for(raw: Option<OsString>, app: Option<&str>) -> Option<PathBuf> {
    let dir = PathBuf::from(raw.filter(|dir| !dir.is_empty())?);
    Some(match app {
        Some(app) => dir.join("app").join(app),
        None => dir,
    })
}

/// The real home's autostart entry, which the Background portal writes on the host.
pub fn host_autostart_entry(app_id: &str) -> PathBuf {
    crate::home()
        .join(".config/autostart")
        .join(format!("{app_id}.desktop"))
}

/// The real home's `.local/share`; the sandbox's XDG_DATA_HOME is under ~/.var/app.
pub fn host_data_home() -> PathBuf {
    crate::home().join(".local/share")
}

/// `command` as the host runs it, for an Exec line written outside the sandbox.
pub fn host_exec(command: &str, app: &str) -> String {
    format!("flatpak run --command={command} {app}")
}

/// `command` in a sandbox of its own, of the newest installed build, not this one's.
pub fn latest_instance(command: &str) -> std::process::Command {
    let mut spawn = std::process::Command::new("flatpak-spawn");
    spawn.args(["--latest-version", command]);
    spawn
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_app_id_comes_from_the_info_file_only_inside_a_sandbox() {
        let dir = std::env::temp_dir().join(format!("vinowhisper-sandbox-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let info = dir.join("flatpak-info");
        assert_eq!(read_id(&info, Some("dev.example.App".into())), None);
        std::fs::write(
            &info,
            "[Application]\nname=dev.example.App\nruntime=x\n\n[Instance]\nname=other\n",
        )
        .unwrap();
        assert_eq!(read_id(&info, None).as_deref(), Some("dev.example.App"));
        assert_eq!(
            read_id(&info, Some("dev.example.Env".into())).as_deref(),
            Some("dev.example.Env")
        );
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn info_values_come_from_their_own_section() {
        let info = "[Application]\nname=a.b\n\n[Instance]\nbranch=stable\napp-path=/x/app/a.b/files\nname=\n";
        assert_eq!(
            ini_value(info, "Instance", "branch").as_deref(),
            Some("stable")
        );
        assert_eq!(ini_value(info, "Application", "branch"), None);
        assert_eq!(ini_value(info, "Instance", "name"), None);
        assert_eq!(
            ini_value("[Instance\nbranch=stable\n", "Instance", "branch"),
            None
        );
    }

    #[test]
    fn flatpak_lists_keep_their_semicolons_and_hashes() {
        let context = "[Context]\nfilesystems=xdg-run/pipewire-0;home;\n# a comment\n[Instance]\napp-path=/a:b #1/files\n";
        assert_eq!(
            ini_value(context, "Context", "filesystems").as_deref(),
            Some("xdg-run/pipewire-0;home;")
        );
        assert_eq!(
            ini_value(context, "Instance", "app-path").as_deref(),
            Some("/a:b #1/files")
        );
    }

    #[test]
    fn the_runtime_dir_is_the_shared_part_inside_a_sandbox() {
        let raw = || Some(OsString::from("/run/user/1000"));
        assert_eq!(
            runtime_dir_for(raw(), None),
            Some(PathBuf::from("/run/user/1000"))
        );
        assert_eq!(
            runtime_dir_for(raw(), Some("a.b.c")),
            Some(PathBuf::from("/run/user/1000/app/a.b.c"))
        );
        assert_eq!(runtime_dir_for(None, Some("a.b.c")), None);
        assert_eq!(runtime_dir_for(Some(OsString::new()), None), None);
    }

    #[test]
    fn the_host_runs_a_command_through_flatpak() {
        assert_eq!(
            host_exec("vinowhisper-gui", "a.b.c"),
            "flatpak run --command=vinowhisper-gui a.b.c"
        );
    }
}
