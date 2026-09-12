//! `--install` and `--uninstall`: a launcher entry, an icon, and optionally
//! a login autostart. All per-user, nothing needs root.
//!
//! The desktop file is more than a menu entry: the global shortcut cannot
//! exist without it (see `ensure_launcher`).

use std::io;
use std::path::{Path, PathBuf};

use crate::APP_ID;
use crate::icon;
use crate::settings;

struct Dirs {
    data: PathBuf,
    config: PathBuf,
}

impl Dirs {
    fn from_env() -> Dirs {
        Dirs {
            data: settings::data_home(),
            config: settings::config_home(),
        }
    }

    fn launcher(&self) -> PathBuf {
        self.data.join(format!("applications/{APP_ID}.desktop"))
    }

    fn icon(&self) -> PathBuf {
        self.data
            .join(format!("icons/hicolor/scalable/apps/{APP_ID}.svg"))
    }

    fn autostart(&self) -> PathBuf {
        self.config.join(format!("autostart/{APP_ID}.desktop"))
    }
}

pub fn install(autostart: bool, caption: Option<&Path>) -> io::Result<Vec<PathBuf>> {
    install_into(
        &Dirs::from_env(),
        &std::env::current_exe()?,
        autostart,
        caption,
    )
}

pub fn uninstall() -> io::Result<Vec<PathBuf>> {
    uninstall_from(&Dirs::from_env())
}

/// The launcher and icon, written only if the launcher is missing. Returns
/// the launcher's path when it had to be written.
///
/// Measured 2026-09-12 on Plasma 6.7: with no desktop file, the portal
/// registry refuses the app id ("App info not found for
/// 'io.github.karanshukla.vinowhisper'"), and the GlobalShortcuts portal then
/// refuses the shortcut outright ("An app id is required"). So a first run
/// that never saw `--install` would get no shortcut at all. An existing
/// launcher is left alone, since `--install` may have baked a `--caption`
/// path into it.
pub fn ensure_launcher() -> io::Result<Option<PathBuf>> {
    ensure_launcher_in(&Dirs::from_env(), &std::env::current_exe()?)
}

fn ensure_launcher_in(dirs: &Dirs, exe: &Path) -> io::Result<Option<PathBuf>> {
    if dirs.launcher().exists() {
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
        written.push(write(
            &dirs.autostart(),
            &desktop_entry(exe, caption, true),
        )?);
    } else if dirs.autostart().exists() {
        // Re-running --install without --autostart is how it gets turned off.
        std::fs::remove_file(dirs.autostart())?;
    }
    Ok(written)
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
    fn reinstalling_without_autostart_turns_it_off() {
        let dirs = scratch("autostart-off");
        install_into(&dirs, Path::new("/bin/vinowhisper-gui"), true, None).unwrap();
        install_into(&dirs, Path::new("/bin/vinowhisper-gui"), false, None).unwrap();
        assert!(!dirs.autostart().exists());
        assert!(dirs.launcher().exists());
    }
}
