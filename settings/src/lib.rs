use std::io;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Source {
    #[default]
    Output,
    Mic,
}

impl Source {
    pub const ALL: [Source; 2] = [Source::Output, Source::Mic];

    pub fn as_arg(self) -> &'static str {
        match self {
            Source::Output => "output",
            Source::Mic => "mic",
        }
    }

    pub fn parse(value: &str) -> Option<Source> {
        Source::ALL
            .into_iter()
            .find(|source| source.as_arg() == value)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Position {
    #[default]
    Bottom,
    Top,
}

impl Position {
    pub const ALL: [Position; 2] = [Position::Bottom, Position::Top];
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum TextSize {
    Small,
    #[default]
    Medium,
    Large,
}

impl TextSize {
    pub const ALL: [TextSize; 3] = [TextSize::Small, TextSize::Medium, TextSize::Large];

    pub fn caption_px(self) -> f32 {
        match self {
            TextSize::Small => 20.0,
            TextSize::Medium => 26.0,
            TextSize::Large => 34.0,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Language {
    #[default]
    En,
    Fr,
    De,
    Es,
    Auto,
}

impl Language {
    pub const ALL: [Language; 5] = [
        Language::En,
        Language::Fr,
        Language::De,
        Language::Es,
        Language::Auto,
    ];
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Task {
    #[default]
    Transcribe,
    Translate,
}

/// What the server listens for. Not in gui.json: `vinowhisper-server` and
/// `vinowhisper-setup` read and write the same file (config.LANGUAGE_FILE).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(default)]
pub struct Speech {
    pub language: Language,
    pub task: Task,
}

impl Speech {
    pub fn path() -> PathBuf {
        config_home().join("vinowhisper/language.json")
    }

    pub fn load() -> Speech {
        Self::load_from(&Self::path())
    }

    pub fn load_from(path: &Path) -> Speech {
        let Ok(text) = std::fs::read_to_string(path) else {
            return Speech::default();
        };
        // The server falls back to English on a bad file too, so both agree on what is live.
        serde_json::from_str::<Speech>(&text)
            .map(Speech::normalised)
            .unwrap_or_default()
    }

    /// English audio has nothing to translate; the server rejects that pair.
    pub fn normalised(mut self) -> Speech {
        if self.language == Language::En {
            self.task = Task::Transcribe;
        }
        self
    }

    pub fn save(&self) {
        let path = Self::path();
        if let Err(err) = self.save_to(&path) {
            eprintln!("[vinowhisper] could not save {}: {err}", path.display());
        }
    }

    pub fn save_to(&self, path: &Path) -> io::Result<()> {
        write_json(path, self)
    }
}

pub const DEFAULT_CLEANUP_BUDGET_S: f64 = 4.0;

/// When a dictation is sent for cleanup. Smart skips the model when the transcript
/// shows nothing to fix, which is most of them and saves about two seconds each.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum CleanupMode {
    #[default]
    Smart,
    Always,
}

impl CleanupMode {
    pub const ALL: [CleanupMode; 2] = [CleanupMode::Smart, CleanupMode::Always];
}

/// LLM cleanup of dictation (issue #81), run by a local model. Not in gui.json: the
/// Python side reads this file at each key press (config.CLEANUP_FILE).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct Cleanup {
    pub enabled: bool,
    pub budget_s: f64,
    pub mode: CleanupMode,
}

impl Default for Cleanup {
    fn default() -> Self {
        Cleanup {
            enabled: false,
            budget_s: DEFAULT_CLEANUP_BUDGET_S,
            mode: CleanupMode::default(),
        }
    }
}

impl Cleanup {
    pub fn path() -> PathBuf {
        config_home().join("vinowhisper/cleanup.json")
    }

    pub fn load() -> Cleanup {
        Self::load_from(&Self::path())
    }

    pub fn load_from(path: &Path) -> Cleanup {
        let Ok(text) = std::fs::read_to_string(path) else {
            return Cleanup::default();
        };
        serde_json::from_str(&text).unwrap_or_else(|err| {
            eprintln!("[vinowhisper] ignoring {}: {err}", path.display());
            Cleanup::default()
        })
    }

    pub fn save(&self) {
        let path = Self::path();
        if let Err(err) = self.save_to(&path) {
            eprintln!("[vinowhisper] could not save {}: {err}", path.display());
        }
    }

    pub fn save_to(&self, path: &Path) -> io::Result<()> {
        write_json(path, self)
    }
}

/// Whether the multilingual export exists (either variant), so the tray can say what to run
/// instead of offering a language the server could not load.
pub fn multilingual_installed() -> bool {
    ["whisper-small-ov", "whisper-small-ov-stateful"]
        .into_iter()
        .map(|name| data_home().join("vinowhisper/models").join(name))
        .any(|dir| {
            std::fs::read_dir(dir).is_ok_and(|mut entries| {
                entries.any(|entry| {
                    entry.is_ok_and(|entry| entry.path().extension().is_some_and(|e| e == "xml"))
                })
            })
        })
}

// The settings window's Wayland app id; the tray installs a desktop entry under it for the icon.
pub const SETTINGS_APP_ID: &str = "io.github.karanshukla.vinowhisper.settings";

pub const DEFAULT_SHORTCUT: &str = "LOGO+ALT+C";

pub const DEFAULT_TRAY_IDLE_MINUTES: u64 = 30;

pub const DEFAULT_HIDE_AFTER_SILENCE_S: u64 = 15;

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct Settings {
    pub source: Source,
    pub position: Position,
    pub size: TextSize,
    pub shortcut: String,
    pub tray_idle_minutes: u64,
    /// Seconds of silence before the captions hide themselves; 0 keeps them up.
    pub hide_after_silence_s: u64,
    pub save_transcripts: bool,
}

impl Default for Settings {
    fn default() -> Self {
        Settings {
            source: Source::default(),
            position: Position::default(),
            size: TextSize::default(),
            shortcut: DEFAULT_SHORTCUT.to_owned(),
            tray_idle_minutes: DEFAULT_TRAY_IDLE_MINUTES,
            hide_after_silence_s: DEFAULT_HIDE_AFTER_SILENCE_S,
            save_transcripts: false,
        }
    }
}

/// What a reload has to do, given the file as it was and as it is now.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct Changes {
    pub source: bool,
    /// Position or size: the overlay has to be placed again.
    pub placement: bool,
    pub tray_idle: bool,
    /// Only a hint to the portal on the first bind; see `Settings::shortcut`.
    pub shortcut: bool,
}

impl Settings {
    pub fn changes_from(&self, old: &Settings) -> Changes {
        Changes {
            source: self.source != old.source,
            placement: self.position != old.position || self.size != old.size,
            tray_idle: self.tray_idle_minutes != old.tray_idle_minutes,
            shortcut: self.shortcut != old.shortcut,
        }
    }

    pub fn path() -> PathBuf {
        config_home().join("vinowhisper/gui.json")
    }

    pub fn load() -> Settings {
        Self::load_from(&Self::path())
    }

    pub fn load_from(path: &Path) -> Settings {
        let Ok(text) = std::fs::read_to_string(path) else {
            return Settings::default();
        };
        serde_json::from_str(&text).unwrap_or_else(|err| {
            eprintln!("[vinowhisper] ignoring {}: {err}", path.display());
            Settings::default()
        })
    }

    pub fn save(&self) {
        let path = Self::path();
        if let Err(err) = self.save_to(&path) {
            eprintln!("[vinowhisper] could not save {}: {err}", path.display());
        }
    }

    pub fn save_to(&self, path: &Path) -> io::Result<()> {
        write_json(path, self)
    }
}

pub fn home() -> PathBuf {
    std::env::var_os("HOME").map_or_else(|| PathBuf::from("/"), PathBuf::from)
}

// Renamed over the file, so a reader or a crash never sees half of one: every
// loader reads a broken file as defaults, silently.
fn write_json<T: Serialize>(path: &Path, value: &T) -> io::Result<()> {
    // Through a symlink, so a dotfiles-managed link stays a link.
    let path = std::fs::canonicalize(path).unwrap_or_else(|_| path.to_path_buf());
    let dir = path.parent().unwrap_or(Path::new("."));
    std::fs::create_dir_all(dir)?;
    let text = serde_json::to_string_pretty(value).map_err(io::Error::other)? + "\n";
    let name = path.file_name().unwrap_or_default().to_string_lossy();
    let partial = dir.join(format!(".{name}.{}.partial", std::process::id()));
    let written = std::fs::File::create(&partial).and_then(|mut file| {
        io::Write::write_all(&mut file, text.as_bytes())?;
        file.sync_all()
    });
    match written.and_then(|()| std::fs::rename(&partial, &path)) {
        Ok(()) => Ok(()),
        Err(err) => {
            let _ = std::fs::remove_file(&partial);
            Err(err)
        }
    }
}

fn xdg_dir(variable: &str, fallback: &str) -> PathBuf {
    match std::env::var_os(variable) {
        Some(dir) if !dir.is_empty() => PathBuf::from(dir),
        _ => home().join(fallback),
    }
}

pub fn config_home() -> PathBuf {
    xdg_dir("XDG_CONFIG_HOME", ".config")
}

pub fn data_home() -> PathBuf {
    xdg_dir("XDG_DATA_HOME", ".local/share")
}

pub fn state_home() -> PathBuf {
    xdg_dir("XDG_STATE_HOME", ".local/state")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn scratch(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "vinowhisper-settings-test-{}-{name}",
            std::process::id()
        ));
        let _ = std::fs::remove_dir_all(&dir);
        dir.join("gui.json")
    }

    #[test]
    fn settings_survive_a_round_trip() {
        let path = scratch("round-trip");
        let settings = Settings {
            source: Source::Mic,
            position: Position::Top,
            size: TextSize::Large,
            shortcut: "CTRL+ALT+K".into(),
            tray_idle_minutes: 5,
            hide_after_silence_s: 40,
            save_transcripts: true,
        };
        settings.save_to(&path).unwrap();
        assert_eq!(Settings::load_from(&path), settings);
    }

    #[test]
    fn a_reload_names_only_what_changed() {
        let old = Settings::default();
        assert_eq!(old.changes_from(&old), Changes::default());

        let new = Settings {
            size: TextSize::Large,
            tray_idle_minutes: 0,
            // not acted on live: save_transcripts is read when the next session starts
            save_transcripts: true,
            ..Settings::default()
        };
        assert_eq!(
            new.changes_from(&old),
            Changes {
                placement: true,
                tray_idle: true,
                ..Changes::default()
            }
        );
        let moved = Settings {
            source: Source::Mic,
            position: Position::Top,
            shortcut: "CTRL+ALT+K".into(),
            ..Settings::default()
        };
        assert_eq!(
            moved.changes_from(&old),
            Changes {
                source: true,
                placement: true,
                shortcut: true,
                tray_idle: false,
            }
        );
    }

    #[test]
    fn a_save_replaces_the_file_rather_than_rewriting_it() {
        use std::os::unix::fs::MetadataExt;
        let path = scratch("atomic");
        Settings::default().save_to(&path).unwrap();
        let before = std::fs::metadata(&path).unwrap().ino();
        let changed = Settings {
            size: TextSize::Large,
            ..Settings::default()
        };
        changed.save_to(&path).unwrap();
        // A new inode: renamed into place, so no reader saw it truncated.
        assert_ne!(std::fs::metadata(&path).unwrap().ino(), before);
        assert_eq!(Settings::load_from(&path), changed);
        let names: Vec<_> = std::fs::read_dir(path.parent().unwrap())
            .unwrap()
            .map(|entry| entry.unwrap().file_name())
            .collect();
        assert_eq!(names, ["gui.json"]);
    }

    #[test]
    fn a_save_through_a_symlink_keeps_the_link() {
        let path = scratch("symlink");
        let dir = path.parent().unwrap();
        let real = dir.join("dotfiles/gui.json");
        Settings::default().save_to(&real).unwrap();
        std::os::unix::fs::symlink(&real, &path).unwrap();
        let changed = Settings {
            position: Position::Top,
            ..Settings::default()
        };
        changed.save_to(&path).unwrap();
        assert!(
            std::fs::symlink_metadata(&path)
                .unwrap()
                .file_type()
                .is_symlink()
        );
        assert_eq!(Settings::load_from(&real), changed);
    }

    #[test]
    fn a_failed_save_leaves_the_old_file_and_no_partial() {
        let path = scratch("failed");
        Settings::default().save_to(&path).unwrap();
        // A directory where the file should be: the rename fails.
        let blocked = path.parent().unwrap().join("blocked.json");
        std::fs::create_dir_all(blocked.join("inside")).unwrap();
        assert!(Settings::default().save_to(&blocked).is_err());
        let partials = std::fs::read_dir(path.parent().unwrap())
            .unwrap()
            .filter(|entry| {
                entry
                    .as_ref()
                    .unwrap()
                    .file_name()
                    .to_string_lossy()
                    .ends_with(".partial")
            })
            .count();
        assert_eq!(partials, 0);
        assert_eq!(Settings::load_from(&path), Settings::default());
    }

    #[test]
    fn a_missing_file_is_the_defaults() {
        assert_eq!(
            Settings::load_from(&scratch("missing")),
            Settings::default()
        );
    }

    #[test]
    fn a_partial_file_keeps_the_rest_at_their_defaults() {
        let path = scratch("partial");
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(&path, r#"{"size": "small"}"#).unwrap();
        let settings = Settings::load_from(&path);
        assert_eq!(settings.size, TextSize::Small);
        assert_eq!(settings.source, Source::Output);
        assert_eq!(settings.shortcut, DEFAULT_SHORTCUT);
        assert_eq!(settings.tray_idle_minutes, DEFAULT_TRAY_IDLE_MINUTES);
        assert_eq!(settings.hide_after_silence_s, DEFAULT_HIDE_AFTER_SILENCE_S);
        assert!(!settings.save_transcripts);
    }

    #[test]
    fn saving_transcripts_is_off_until_the_file_says_so() {
        assert!(!Settings::default().save_transcripts);
        let path = scratch("transcripts");
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(&path, r#"{"save_transcripts": true}"#).unwrap();
        assert!(Settings::load_from(&path).save_transcripts);
    }

    #[test]
    fn the_tray_idle_time_is_read_from_the_file() {
        let path = scratch("tray-idle");
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(&path, r#"{"tray_idle_minutes": 0}"#).unwrap();
        assert_eq!(Settings::load_from(&path).tray_idle_minutes, 0);
    }

    #[test]
    fn captions_hide_after_fifteen_seconds_of_silence_unless_the_file_says_otherwise() {
        assert_eq!(Settings::default().hide_after_silence_s, 15);
        let path = scratch("hide-after");
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(&path, r#"{"hide_after_silence_s": 0}"#).unwrap();
        assert_eq!(Settings::load_from(&path).hide_after_silence_s, 0);
    }

    #[test]
    fn a_corrupt_file_does_not_stop_captions() {
        let path = scratch("corrupt");
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(&path, "{not json").unwrap();
        assert_eq!(Settings::load_from(&path), Settings::default());
    }

    #[test]
    fn speech_matches_the_file_the_server_reads() {
        let path = scratch("speech");
        let speech = Speech {
            language: Language::Fr,
            task: Task::Translate,
        };
        speech.save_to(&path).unwrap();
        assert_eq!(
            std::fs::read_to_string(&path)
                .unwrap()
                .replace([' ', '\n'], ""),
            r#"{"language":"fr","task":"translate"}"#
        );
        assert_eq!(Speech::load_from(&path), speech);
    }

    #[test]
    fn speech_falls_back_to_english_like_the_server() {
        let path = scratch("speech-bad");
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        assert_eq!(Speech::load_from(&path), Speech::default());
        std::fs::write(&path, r#"{"language": "xx"}"#).unwrap();
        assert_eq!(Speech::load_from(&path), Speech::default());
        std::fs::write(&path, r#"{"language": "en", "task": "translate"}"#).unwrap();
        assert_eq!(Speech::load_from(&path), Speech::default());
    }

    #[test]
    fn choosing_english_drops_translate() {
        let speech = Speech {
            language: Language::En,
            task: Task::Translate,
        };
        assert_eq!(speech.normalised().task, Task::Transcribe);
    }

    #[test]
    fn cleanup_is_off_until_the_file_says_otherwise() {
        assert!(!Cleanup::default().enabled);
        let path = scratch("cleanup-partial");
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(&path, r#"{"enabled": true}"#).unwrap();
        let loaded = Cleanup::load_from(&path);
        assert!(loaded.enabled);
        assert_eq!(loaded.budget_s, DEFAULT_CLEANUP_BUDGET_S);
        assert_eq!(loaded.mode, CleanupMode::Smart);
    }

    #[test]
    fn a_file_from_the_hosted_endpoint_days_still_loads() {
        let path = scratch("cleanup-legacy");
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(
            &path,
            r#"{"enabled": true, "url": "https://api.anthropic.com", "model": "m", "budget_s": 2.5, "mode": "always"}"#,
        )
        .unwrap();
        let loaded = Cleanup::load_from(&path);
        assert!(loaded.enabled);
        assert_eq!(loaded.budget_s, 2.5);
        assert_eq!(loaded.mode, CleanupMode::Always);
    }

    #[test]
    fn cleanup_matches_the_file_the_dictation_process_reads() {
        let path = scratch("cleanup");
        let cleanup = Cleanup {
            enabled: true,
            budget_s: 1.5,
            mode: CleanupMode::Always,
        };
        cleanup.save_to(&path).unwrap();
        let text = std::fs::read_to_string(&path).unwrap();
        for key in ["enabled", "budget_s", "mode"] {
            assert!(text.contains(&format!("\"{key}\"")), "{key}");
        }
        for gone in ["url", "model"] {
            assert!(!text.contains(&format!("\"{gone}\"")), "{gone}");
        }
        assert_eq!(Cleanup::load_from(&path), cleanup);
    }

    #[test]
    fn sources_are_named_the_way_the_caption_cli_takes_them() {
        assert_eq!(Source::parse("output"), Some(Source::Output));
        assert_eq!(Source::parse("mic"), Some(Source::Mic));
        assert_eq!(Source::parse("speakers"), None);
    }
}
