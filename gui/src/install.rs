//! Desktop integration: a launcher entry, an icon, and a login autostart.
//!
//! `--install` writes the launcher and icon for a per-user copy, `--autostart`
//! the login entry (on its own, for a packaged copy whose launcher came with
//! the package), `--uninstall` removes what those wrote, and
//! `--export-desktop DIR` writes the launcher and icon into a package's
//! buildroot. None of it needs root.
//!
//! The desktop file is more than a menu entry: the global shortcut cannot
//! exist without it (see `ensure_launcher`).

use std::io;
use std::path::{Path, PathBuf};

use crate::APP_ID;
use crate::icon;
use crate::settings;

/// What a package puts on PATH, and so what a packaged launcher names
/// instead of a path.
const COMMAND: &str = "vinowhisper-gui";

struct Dirs {
    data: PathBuf,
    config: PathBuf,
    /// `$XDG_DATA_DIRS`, where a package's launcher lives.
    system: Vec<PathBuf>,
}

impl Dirs {
    fn from_env() -> Dirs {
        let system = match std::env::var_os("XDG_DATA_DIRS") {
            Some(dirs) if !dirs.is_empty() => std::env::split_paths(&dirs).collect(),
            _ => vec!["/usr/local/share".into(), "/usr/share".into()],
        };
        Dirs {
            data: settings::data_home(),
            config: settings::config_home(),
            system,
        }
    }

    fn launcher(&self) -> PathBuf {
        launcher_in(&self.data)
    }

    fn icon(&self) -> PathBuf {
        icon_in(&self.data)
    }

    fn autostart(&self) -> PathBuf {
        self.config.join(format!("autostart/{APP_ID}.desktop"))
    }

    fn packaged_launcher(&self) -> Option<PathBuf> {
        self.system
            .iter()
            .map(|dir| launcher_in(dir))
            .find(|path| path.exists())
    }
}

fn launcher_in(data: &Path) -> PathBuf {
    data.join(format!("applications/{APP_ID}.desktop"))
}

fn icon_in(data: &Path) -> PathBuf {
    data.join(format!("icons/hicolor/scalable/apps/{APP_ID}.svg"))
}

pub fn install(autostart: bool, caption: Option<&Path>) -> io::Result<Vec<PathBuf>> {
    install_into(
        &Dirs::from_env(),
        &std::env::current_exe()?,
        autostart,
        caption,
    )
}

/// The login entry alone, for a copy whose launcher came from a package.
pub fn autostart(caption: Option<&Path>) -> io::Result<Vec<PathBuf>> {
    autostart_into(&Dirs::from_env(), &std::env::current_exe()?, caption)
}

pub fn uninstall() -> io::Result<Vec<PathBuf>> {
    uninstall_from(&Dirs::from_env())
}

/// For packagers: the launcher and icon under `data_dir` (a buildroot's
/// /usr/share). The launcher names the command rather than a path, since a
/// package puts it on PATH and the buildroot path would be wrong once
/// installed.
pub fn export(data_dir: &Path) -> io::Result<Vec<PathBuf>> {
    Ok(vec![
        write(
            &launcher_in(data_dir),
            &desktop_entry(Path::new(COMMAND), None, false),
        )?,
        write(&icon_in(data_dir), icon::APP_SVG)?,
    ])
}

/// The launcher and icon, written only if there is no launcher yet, per-user
/// or packaged. Returns the launcher's path when it had to be written.
///
/// Measured 2026-09-12 on Plasma 6.7: with no desktop file, the portal
/// registry refuses the app id ("App info not found for
/// 'io.github.karanshukla.vinowhisper'"), and the GlobalShortcuts portal then
/// refuses the shortcut outright ("An app id is required"). So a first run
/// that never saw `--install` would get no shortcut at all. An existing
/// launcher is left alone: `--install` may have baked a `--caption` path into
/// it, and a packaged one must not be shadowed by a copy in the home
/// directory.
pub fn ensure_launcher() -> io::Result<Option<PathBuf>> {
    ensure_launcher_in(&Dirs::from_env(), &std::env::current_exe()?)
}

fn ensure_launcher_in(dirs: &Dirs, exe: &Path) -> io::Result<Option<PathBuf>> {
    if dirs.launcher().exists() || dirs.packaged_launcher().is_some() {
        return Ok(None);
    }
    write(&dirs.icon(), icon::APP_SVG)?;
    write(&dirs.launcher(), &desktop_entry(exe, None, false)).map(Some)
}

fn install_into(
    dirs: &Dirs,
    exe: &Path,
    autostart: bool,
    caption: Option<&Path>,
) -> io::Result<Vec<PathBuf>> {
    let mut written = vec![
        write(&dirs.launcher(), &desktop_entry(exe, caption, false))?,
        write(&dirs.icon(), icon::APP_SVG)?,
    ];
    if autostart {
        written.extend(autostart_into(dirs, exe, caption)?);
    } else if dirs.autostart().exists() {
        // Re-running --install without --autostart is how it gets turned off.
        std::fs::remove_file(dirs.autostart())?;
    }
    Ok(written)
}

fn autostart_into(dirs: &Dirs, exe: &Path, caption: Option<&Path>) -> io::Result<Vec<PathBuf>> {
    Ok(vec![write(
        &dirs.autostart(),
        &desktop_entry(exe, caption, true),
    )?])
}

fn uninstall_from(dirs: &Dirs) -> io::Result<Vec<PathBuf>> {
    let mut removed = Vec::new();
    for path in [dirs.launcher(), dirs.icon(), dirs.autostart()] {
        match std::fs::remove_file(&path) {
            Ok(()) => removed.push(path),
            Err(err) if err.kind() == io::ErrorKind::NotFound => {}
            Err(err) => return Err(err),
        }
    }
    Ok(removed)
}

fn write(path: &Path, contents: &str) -> io::Result<PathBuf> {
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir)?;
    }
    std::fs::write(path, contents)?;
    Ok(path.to_owned())
}

/// The launcher, or with `autostart` its login twin, which starts in the tray
/// without showing captions (and so without touching the NPU until asked).
fn desktop_entry(exe: &Path, caption: Option<&Path>, autostart: bool) -> String {
    let mut exec = exec_arg(&exe.to_string_lossy());
    if let Some(caption) = caption {
        exec.push_str(" --caption ");
        exec.push_str(&exec_arg(&caption.to_string_lossy()));
    }
    let (launch, extra) = if autostart {
        (
            format!("{exec} --hidden"),
            "X-GNOME-Autostart-enabled=true\n",
        )
    } else {
        (exec.clone(), "")
    };
    format!(
        "[Desktop Entry]
Type=Application
Name=vinoWhisper Captions
GenericName=Live Captions
Comment=Live captions for anything playing, transcribed on the NPU
Exec={launch}
Icon={APP_ID}
Terminal=false
Categories=AudioVideo;Audio;Utility;Accessibility;
Keywords=captions;subtitles;transcription;speech;whisper;
StartupNotify=false
{extra}Actions=toggle;

[Desktop Action toggle]
Name=Show or Hide Captions
Exec={exec} toggle
"
    )
}

/// One argument of an `Exec=` line, quoted by the Desktop Entry spec's rules
/// when it has to be. `%` is a field code there, so it is doubled either way.
fn exec_arg(arg: &str) -> String {
    const RESERVED: &[char] = &[
        ' ', '\t', '\n', '"', '\'', '\\', '>', '<', '~', '|', '&', ';', '$', '*', '?', '#', '(',
        ')', '`',
    ];
    if !arg.contains(RESERVED) {
        return arg.replace('%', "%%");
    }
    let mut quoted = String::from("\"");
    for c in arg.chars() {
        match c {
            '"' | '`' | '$' => {
                quoted.push_str("\\\\");
                quoted.push(c);
            }
            // Escaped once for the quoting and again for the key file.
            '\\' => quoted.push_str("\\\\\\\\"),
            '%' => quoted.push_str("%%"),
            _ => quoted.push(c),
        }
    }
    quoted.push('"');
    quoted
}

#[cfg(test)]
mod tests {
    use super::*;

    fn scratch(name: &str) -> Dirs {
        let root = std::env::temp_dir().join(format!(
            "vinowhisper-gui-install-{}-{name}",
            std::process::id()
        ));
        let _ = std::fs::remove_dir_all(&root);
        Dirs {
            data: root.join("data"),
            config: root.join("config"),
            system: vec![root.join("usr/share")],
        }
    }

    fn exec_line(entry: &str) -> &str {
        entry
            .lines()
            .find(|line| line.starts_with("Exec="))
            .unwrap()
    }

    #[test]
    fn the_launcher_runs_this_binary_by_absolute_path() {
        let entry = desktop_entry(
            Path::new("/home/me/.local/bin/vinowhisper-gui"),
            None,
            false,
        );
        assert_eq!(
            exec_line(&entry),
            "Exec=/home/me/.local/bin/vinowhisper-gui"
        );
        assert!(entry.contains(&format!("Icon={APP_ID}")));
        assert!(entry.contains("Exec=/home/me/.local/bin/vinowhisper-gui toggle"));
    }

    #[test]
    fn autostart_starts_in_the_tray() {
        let entry = desktop_entry(Path::new("/bin/vinowhisper-gui"), None, true);
        assert_eq!(exec_line(&entry), "Exec=/bin/vinowhisper-gui --hidden");
    }

    #[test]
    fn an_explicit_caption_path_is_baked_in() {
        let entry = desktop_entry(
            Path::new("/bin/vinowhisper-gui"),
            Some(Path::new("/opt/venv/bin/vinowhisper-caption")),
            false,
        );
        assert_eq!(
            exec_line(&entry),
            "Exec=/bin/vinowhisper-gui --caption /opt/venv/bin/vinowhisper-caption"
        );
    }

    #[test]
    fn paths_with_spaces_are_quoted_and_percent_is_escaped() {
        assert_eq!(exec_arg("/plain/path"), "/plain/path");
        assert_eq!(exec_arg("/My Apps/gui"), "\"/My Apps/gui\"");
        assert_eq!(exec_arg("/100%/gui"), "/100%%/gui");
    }

    #[test]
    fn install_then_uninstall_leaves_nothing_behind() {
        let dirs = scratch("round-trip");
        let written = install_into(&dirs, Path::new("/bin/vinowhisper-gui"), true, None).unwrap();
        assert_eq!(written.len(), 3);
        assert!(written.iter().all(|path| path.exists()));

        let removed = uninstall_from(&dirs).unwrap();
        assert_eq!(removed.len(), 3);
        assert!(written.iter().all(|path| !path.exists()));
    }

    #[test]
    fn a_first_run_writes_the_launcher_once_and_never_overwrites_it() {
        let dirs = scratch("ensure");
        let written = ensure_launcher_in(&dirs, Path::new("/first/vinowhisper-gui")).unwrap();
        assert_eq!(written, Some(dirs.launcher()));
        assert!(dirs.icon().exists());

        // A later run from elsewhere must not replace what is there, which
        // may be an --install with a --caption path in it.
        let again = ensure_launcher_in(&dirs, Path::new("/second/vinowhisper-gui")).unwrap();
        assert_eq!(again, None);
        let entry = std::fs::read_to_string(dirs.launcher()).unwrap();
        assert!(entry.contains("Exec=/first/vinowhisper-gui"));
    }

    #[test]
    fn a_packaged_launcher_is_not_shadowed_by_a_first_run() {
        let dirs = scratch("packaged");
        export(&dirs.system[0]).unwrap();
        assert_eq!(
            ensure_launcher_in(&dirs, Path::new("/usr/bin/vinowhisper-gui")).unwrap(),
            None
        );
        assert!(!dirs.launcher().exists());
    }

    #[test]
    fn an_exported_launcher_names_the_command_not_a_buildroot_path() {
        let dirs = scratch("export");
        let written = export(&dirs.data).unwrap();
        assert_eq!(written, vec![dirs.launcher(), dirs.icon()]);
        let entry = std::fs::read_to_string(dirs.launcher()).unwrap();
        assert_eq!(exec_line(&entry), "Exec=vinowhisper-gui");
    }

    #[test]
    fn autostart_on_its_own_writes_only_the_login_entry() {
        let dirs = scratch("autostart-only");
        let written = autostart_into(&dirs, Path::new("/usr/bin/vinowhisper-gui"), None).unwrap();
        assert_eq!(written, vec![dirs.autostart()]);
        assert!(!dirs.launcher().exists());
    }

    #[test]
    fn reinstalling_without_autostart_turns_it_off() {
        let dirs = scratch("autostart-off");
        install_into(&dirs, Path::new("/bin/vinowhisper-gui"), true, None).unwrap();
        install_into(&dirs, Path::new("/bin/vinowhisper-gui"), false, None).unwrap();
        assert!(!dirs.autostart().exists());
        assert!(dirs.launcher().exists());
    }
}
