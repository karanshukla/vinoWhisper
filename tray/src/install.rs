use std::io;
use std::path::{Path, PathBuf};

use crate::APP_ID;
use crate::icon;
use crate::settings;

const COMMAND: &str = "vinowhisper-tray";
const LEGACY_COMMAND: &str = "vinowhisper-gui";
const SETTINGS_COMMAND: &str = "vinowhisper-gui";

struct Dirs {
    data: PathBuf,
    config: PathBuf,
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

    fn icons(&self) -> [(PathBuf, String); 2] {
        icons_in(&self.data)
    }

    fn settings_entry(&self) -> PathBuf {
        settings_entry_in(&self.data)
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

fn settings_entry_in(data: &Path) -> PathBuf {
    data.join(format!(
        "applications/{}.desktop",
        settings::SETTINGS_APP_ID
    ))
}

pub const SESSION_MIME: &str = "application/vnd.vinowhisper.session+zip";

fn mime_package_in(data: &Path) -> PathBuf {
    data.join(format!("mime/packages/{APP_ID}-session.xml"))
}

// The bundle's own type (#123): its icon in file managers, and opening one goes to Sessions.
// The magic is the zip's first entry, "mimetype", stored at offset 30 with its value at 38.
fn mime_package() -> String {
    format!(
        r#"<?xml version="1.0" encoding="UTF-8"?>
<mime-info xmlns="http://www.freedesktop.org/standards/shared-mime-info">
  <mime-type type="{SESSION_MIME}">
    <comment>vinoWhisper session</comment>
    <sub-class-of type="application/zip"/>
    <icon name="{APP_ID}"/>
    <generic-icon name="text-x-generic"/>
    <glob pattern="*.vwsession"/>
    <magic priority="70">
      <match type="string" offset="30" value="mimetype">
        <match type="string" offset="38" value="{SESSION_MIME}"/>
      </match>
    </magic>
  </mime-type>
</mime-info>
"#
    )
}

fn refresh_databases(data: &Path) {
    for (tool, dir) in [
        ("update-mime-database", data.join("mime")),
        ("update-desktop-database", data.join("applications")),
    ] {
        let _ = std::process::Command::new(tool)
            .arg(dir)
            .stdin(std::process::Stdio::null())
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .status();
    }
}

fn settings_command(exe: &Path) -> PathBuf {
    exe.with_file_name(SETTINGS_COMMAND)
}

fn icons_in(data: &Path) -> [(PathBuf, String); 2] {
    let hicolor = data.join("icons/hicolor");
    [
        (
            hicolor.join(format!("scalable/apps/{APP_ID}.svg")),
            icon::app_svg(),
        ),
        (
            hicolor.join(format!("symbolic/apps/{APP_ID}-symbolic.svg")),
            icon::symbolic_svg(),
        ),
    ]
}

fn write_icons(data: &Path) -> io::Result<Vec<PathBuf>> {
    icons_in(data)
        .iter()
        .map(|(path, svg)| write(path, svg))
        .collect()
}

pub fn install(autostart: bool, caption: Option<&Path>) -> io::Result<Vec<PathBuf>> {
    install_into(
        &Dirs::from_env(),
        &std::env::current_exe()?,
        autostart,
        caption,
    )
}

pub fn autostart(caption: Option<&Path>) -> io::Result<Vec<PathBuf>> {
    autostart_into(&Dirs::from_env(), &std::env::current_exe()?, caption)
}

pub fn autostart_enabled() -> bool {
    Dirs::from_env().autostart().exists()
}

pub fn set_autostart(enabled: bool, caption: Option<&Path>) -> io::Result<()> {
    set_autostart_in(
        &Dirs::from_env(),
        &std::env::current_exe()?,
        enabled,
        caption,
    )
    .map(drop)
}

pub fn uninstall() -> io::Result<Vec<PathBuf>> {
    uninstall_from(&Dirs::from_env())
}

pub fn export(data_dir: &Path) -> io::Result<Vec<PathBuf>> {
    let mut written = vec![write(
        &launcher_in(data_dir),
        &desktop_entry(Path::new(COMMAND), None, false),
    )?];
    written.push(write(
        &settings_entry_in(data_dir),
        &settings_desktop_entry(Path::new(SETTINGS_COMMAND)),
    )?);
    written.push(write(&mime_package_in(data_dir), &mime_package())?);
    written.extend(write_icons(data_dir)?);
    Ok(written)
}

pub fn ensure_launcher() -> io::Result<Option<PathBuf>> {
    ensure_launcher_in(&Dirs::from_env(), &std::env::current_exe()?)
}

fn ensure_launcher_in(dirs: &Dirs, exe: &Path) -> io::Result<Option<PathBuf>> {
    let own = dirs.launcher().exists();
    if !own && dirs.packaged_launcher().is_some() {
        return Ok(None);
    }
    for (path, svg) in dirs.icons() {
        if std::fs::read_to_string(&path).ok().as_deref() != Some(svg.as_str()) {
            write(&path, &svg)?;
        }
    }
    let settings_entry = settings_desktop_entry(&settings_command(exe));
    let mut changed = false;
    if std::fs::read_to_string(dirs.settings_entry()).ok() != Some(settings_entry.clone()) {
        write(&dirs.settings_entry(), &settings_entry)?;
        changed = true;
    }
    let mime = mime_package_in(&dirs.data);
    if std::fs::read_to_string(&mime).ok() != Some(mime_package()) {
        write(&mime, &mime_package())?;
        changed = true;
    }
    if changed {
        refresh_databases(&dirs.data);
    }
    migrate_legacy_command(&dirs.launcher())?;
    migrate_legacy_command(&dirs.autostart())?;
    if own {
        return Ok(None);
    }
    write(&dirs.launcher(), &desktop_entry(exe, None, false)).map(Some)
}

// Before 1.0 this binary was vinowhisper-gui; vinowhisper-gui is now the settings window.
fn migrate_legacy_command(path: &Path) -> io::Result<()> {
    let Ok(text) = std::fs::read_to_string(path) else {
        return Ok(());
    };
    let migrated = rename_legacy_command(&text);
    if migrated != text {
        std::fs::write(path, migrated)?;
    }
    Ok(())
}

fn rename_legacy_command(entry: &str) -> String {
    entry
        .split_inclusive('\n')
        .map(|line| {
            if line.starts_with("Exec=") {
                rename_in_exec_line(line)
            } else {
                line.to_owned()
            }
        })
        .collect()
}

fn rename_in_exec_line(line: &str) -> String {
    let mut out = String::with_capacity(line.len());
    let mut rest = line;
    while let Some(at) = rest.find(LEGACY_COMMAND) {
        let end = at + LEGACY_COMMAND.len();
        let ends_the_name = rest[end..]
            .chars()
            .next()
            .is_none_or(|c| matches!(c, ' ' | '"' | '\n'));
        out.push_str(&rest[..at]);
        out.push_str(if ends_the_name {
            COMMAND
        } else {
            LEGACY_COMMAND
        });
        rest = &rest[end..];
    }
    out.push_str(rest);
    out
}

fn install_into(
    dirs: &Dirs,
    exe: &Path,
    autostart: bool,
    caption: Option<&Path>,
) -> io::Result<Vec<PathBuf>> {
    let mut written = vec![write(
        &dirs.launcher(),
        &desktop_entry(exe, caption, false),
    )?];
    written.push(write(
        &dirs.settings_entry(),
        &settings_desktop_entry(&settings_command(exe)),
    )?);
    written.push(write(&mime_package_in(&dirs.data), &mime_package())?);
    written.extend(write_icons(&dirs.data)?);
    written.extend(set_autostart_in(dirs, exe, autostart, caption)?);
    refresh_databases(&dirs.data);
    Ok(written)
}

fn set_autostart_in(
    dirs: &Dirs,
    exe: &Path,
    enabled: bool,
    caption: Option<&Path>,
) -> io::Result<Vec<PathBuf>> {
    if enabled {
        return autostart_into(dirs, exe, caption);
    }
    match std::fs::remove_file(dirs.autostart()) {
        Err(err) if err.kind() != io::ErrorKind::NotFound => Err(err),
        _ => Ok(Vec::new()),
    }
}

fn autostart_into(dirs: &Dirs, exe: &Path, caption: Option<&Path>) -> io::Result<Vec<PathBuf>> {
    Ok(vec![write(
        &dirs.autostart(),
        &desktop_entry(exe, caption, true),
    )?])
}

fn uninstall_from(dirs: &Dirs) -> io::Result<Vec<PathBuf>> {
    let mut removed = Vec::new();
    let paths = [
        dirs.launcher(),
        dirs.settings_entry(),
        mime_package_in(&dirs.data),
    ]
    .into_iter()
    .chain(dirs.icons().map(|(path, _)| path))
    .chain([dirs.autostart()]);
    for path in paths {
        match std::fs::remove_file(&path) {
            Ok(()) => removed.push(path),
            Err(err) if err.kind() == io::ErrorKind::NotFound => {}
            Err(err) => return Err(err),
        }
    }
    refresh_databases(&dirs.data);
    Ok(removed)
}

fn write(path: &Path, contents: &str) -> io::Result<PathBuf> {
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir)?;
    }
    std::fs::write(path, contents)?;
    Ok(path.to_owned())
}

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
Name=vinoWhisper
GenericName=Live Captions and Dictation
Comment=Live captions for anything playing, and voice typing, transcribed on the NPU
Exec={launch}
Icon={APP_ID}
Terminal=false
Categories=AudioVideo;Audio;Utility;Accessibility;
Keywords=captions;subtitles;transcription;speech;whisper;dictation;voice;typing;
StartupNotify=false
{extra}Actions=toggle;

[Desktop Action toggle]
Name=Show or Hide Captions
Exec={exec} toggle
"
    )
}

// Named after the window's app id, which is how the task manager finds its icon.
fn settings_desktop_entry(exe: &Path) -> String {
    format!(
        "[Desktop Entry]
Type=Application
Name=vinoWhisper Settings
GenericName=Captions and Dictation Settings
Comment=Caption position and size, audio source, language, and shortcuts
Exec={} %f
Icon={APP_ID}
Terminal=false
MimeType={SESSION_MIME};
Categories=Settings;Utility;Accessibility;
Keywords=captions;subtitles;dictation;whisper;shortcut;language;preferences;
StartupNotify=false
",
        exec_arg(&exe.to_string_lossy())
    )
}

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
            "vinowhisper-tray-install-{}-{name}",
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
            Path::new("/home/me/.local/bin/vinowhisper-tray"),
            None,
            false,
        );
        assert_eq!(
            exec_line(&entry),
            "Exec=/home/me/.local/bin/vinowhisper-tray"
        );
        assert!(entry.contains(&format!("Icon={APP_ID}")));
        assert!(entry.contains("Exec=/home/me/.local/bin/vinowhisper-tray toggle"));
    }

    #[test]
    fn autostart_starts_in_the_tray() {
        let entry = desktop_entry(Path::new("/bin/vinowhisper-tray"), None, true);
        assert_eq!(exec_line(&entry), "Exec=/bin/vinowhisper-tray --hidden");
    }

    #[test]
    fn an_explicit_caption_path_is_baked_in() {
        let entry = desktop_entry(
            Path::new("/bin/vinowhisper-tray"),
            Some(Path::new("/opt/venv/bin/vinowhisper-caption")),
            false,
        );
        assert_eq!(
            exec_line(&entry),
            "Exec=/bin/vinowhisper-tray --caption /opt/venv/bin/vinowhisper-caption"
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
        let written = install_into(&dirs, Path::new("/bin/vinowhisper-tray"), true, None).unwrap();
        assert_eq!(written.len(), 6);
        assert!(written.iter().all(|path| path.exists()));

        let removed = uninstall_from(&dirs).unwrap();
        assert_eq!(removed.len(), 6);
        assert!(written.iter().all(|path| !path.exists()));
    }

    #[test]
    fn a_first_run_writes_the_launcher_once_and_never_overwrites_it() {
        let dirs = scratch("ensure");
        let written = ensure_launcher_in(&dirs, Path::new("/first/vinowhisper-tray")).unwrap();
        assert_eq!(written, Some(dirs.launcher()));
        assert!(dirs.icons().iter().all(|(path, _)| path.exists()));

        let again = ensure_launcher_in(&dirs, Path::new("/second/vinowhisper-tray")).unwrap();
        assert_eq!(again, None);
        let entry = std::fs::read_to_string(dirs.launcher()).unwrap();
        assert!(entry.contains("Exec=/first/vinowhisper-tray"));
    }

    #[test]
    fn the_settings_window_gets_the_app_icon_even_when_the_launcher_already_exists() {
        let dirs = scratch("settings-entry");
        install_into(&dirs, Path::new("/old/vinowhisper-tray"), false, None).unwrap();
        std::fs::remove_file(dirs.settings_entry()).unwrap();

        ensure_launcher_in(&dirs, Path::new("/home/me/.local/bin/vinowhisper-tray")).unwrap();
        let entry = std::fs::read_to_string(dirs.settings_entry()).unwrap();
        assert_eq!(
            exec_line(&entry),
            "Exec=/home/me/.local/bin/vinowhisper-gui %f"
        );
        assert!(entry.contains(&format!("Icon={APP_ID}")));
        assert!(!entry.contains("NoDisplay"));
        assert!(
            dirs.settings_entry()
                .ends_with(format!("{}.desktop", settings::SETTINGS_APP_ID))
        );
    }

    #[test]
    fn a_packaged_launcher_is_not_shadowed_by_a_first_run() {
        let dirs = scratch("packaged");
        export(&dirs.system[0]).unwrap();
        assert_eq!(
            ensure_launcher_in(&dirs, Path::new("/usr/bin/vinowhisper-tray")).unwrap(),
            None
        );
        assert!(!dirs.launcher().exists());
    }

    #[test]
    fn an_exported_launcher_names_the_command_not_a_buildroot_path() {
        let dirs = scratch("export");
        let written = export(&dirs.data).unwrap();
        let icons = dirs.icons().map(|(path, _)| path);
        assert_eq!(
            written,
            [
                vec![
                    dirs.launcher(),
                    dirs.settings_entry(),
                    mime_package_in(&dirs.data)
                ],
                icons.to_vec()
            ]
            .concat()
        );
        let entry = std::fs::read_to_string(dirs.launcher()).unwrap();
        assert_eq!(exec_line(&entry), "Exec=vinowhisper-tray");
        let settings = std::fs::read_to_string(dirs.settings_entry()).unwrap();
        assert_eq!(exec_line(&settings), "Exec=vinowhisper-gui %f");
    }

    #[test]
    fn a_later_run_updates_stale_icons_but_not_the_launcher() {
        let dirs = scratch("stale-icons");
        install_into(
            &dirs,
            Path::new("/bin/vinowhisper-tray"),
            false,
            Some(Path::new("/opt/venv/bin/vinowhisper-caption")),
        )
        .unwrap();
        let launcher = std::fs::read_to_string(dirs.launcher()).unwrap();
        for (path, _) in dirs.icons() {
            std::fs::write(path, "<svg>an older design</svg>").unwrap();
        }

        assert_eq!(
            ensure_launcher_in(&dirs, Path::new("/elsewhere/vinowhisper-tray")).unwrap(),
            None
        );
        for (path, svg) in dirs.icons() {
            assert_eq!(std::fs::read_to_string(path).unwrap(), svg);
        }
        assert_eq!(std::fs::read_to_string(dirs.launcher()).unwrap(), launcher);
    }

    #[test]
    fn a_launcher_and_login_entry_from_before_the_rename_follow_the_binary() {
        let dirs = scratch("legacy");
        let launcher = "[Desktop Entry]\nExec=/home/me/.local/bin/vinowhisper-gui --caption /v/bin/vinowhisper-caption\nName=vinoWhisper\n\n[Desktop Action toggle]\nExec=/home/me/.local/bin/vinowhisper-gui --caption /v/bin/vinowhisper-caption toggle\n";
        let login = "[Desktop Entry]\nExec=\"/My Apps/vinowhisper-gui\" --hidden\n";
        for (path, text) in [(dirs.launcher(), launcher), (dirs.autostart(), login)] {
            std::fs::create_dir_all(path.parent().unwrap()).unwrap();
            std::fs::write(path, text).unwrap();
        }

        assert_eq!(
            ensure_launcher_in(&dirs, Path::new("/x/vinowhisper-tray")).unwrap(),
            None
        );
        let launcher = std::fs::read_to_string(dirs.launcher()).unwrap();
        assert_eq!(
            exec_line(&launcher),
            "Exec=/home/me/.local/bin/vinowhisper-tray --caption /v/bin/vinowhisper-caption"
        );
        assert!(launcher.contains("vinowhisper-tray --caption /v/bin/vinowhisper-caption toggle"));
        let login = std::fs::read_to_string(dirs.autostart()).unwrap();
        assert_eq!(
            exec_line(&login),
            "Exec=\"/My Apps/vinowhisper-tray\" --hidden"
        );
    }

    #[test]
    fn only_the_command_name_is_renamed() {
        assert_eq!(
            rename_legacy_command(
                "Exec=/src/vinowhisper-gui-checkout/vinowhisper-gui\nName=vinowhisper-gui\n"
            ),
            "Exec=/src/vinowhisper-gui-checkout/vinowhisper-tray\nName=vinowhisper-gui\n"
        );
    }

    #[test]
    fn autostart_on_its_own_writes_only_the_login_entry() {
        let dirs = scratch("autostart-only");
        let written = autostart_into(&dirs, Path::new("/usr/bin/vinowhisper-tray"), None).unwrap();
        assert_eq!(written, vec![dirs.autostart()]);
        assert!(!dirs.launcher().exists());
    }

    #[test]
    fn reinstalling_without_autostart_turns_it_off() {
        let dirs = scratch("autostart-off");
        install_into(&dirs, Path::new("/bin/vinowhisper-tray"), true, None).unwrap();
        install_into(&dirs, Path::new("/bin/vinowhisper-tray"), false, None).unwrap();
        assert!(!dirs.autostart().exists());
        assert!(dirs.launcher().exists());
    }

    #[test]
    fn the_tray_toggle_turns_autostart_on_and_off() {
        let dirs = scratch("autostart-toggle");
        let exe = Path::new("/bin/vinowhisper-tray");
        set_autostart_in(&dirs, exe, false, None).unwrap();
        assert!(!dirs.autostart().exists());

        set_autostart_in(&dirs, exe, true, None).unwrap();
        let entry = std::fs::read_to_string(dirs.autostart()).unwrap();
        assert_eq!(exec_line(&entry), "Exec=/bin/vinowhisper-tray --hidden");

        set_autostart_in(&dirs, exe, false, None).unwrap();
        assert!(!dirs.autostart().exists());
        assert!(!dirs.launcher().exists());
    }

    #[test]
    fn a_session_bundle_opens_in_the_settings_window_by_type() {
        let dirs = scratch("mime");
        ensure_launcher_in(&dirs, Path::new("/home/me/.local/bin/vinowhisper-tray")).unwrap();
        let settings = std::fs::read_to_string(dirs.settings_entry()).unwrap();
        assert!(settings.contains("MimeType=application/vnd.vinowhisper.session+zip;\n"));
        let xml = std::fs::read_to_string(mime_package_in(&dirs.data)).unwrap();
        assert!(xml.contains(r#"<glob pattern="*.vwsession"/>"#));
        assert!(xml.contains(r#"<icon name="io.github.karanshukla.vinowhisper"/>"#));
        assert!(xml.contains(r#"offset="38" value="application/vnd.vinowhisper.session+zip""#));
        uninstall_from(&dirs).unwrap();
        assert!(!mime_package_in(&dirs.data).exists());
    }
}
