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
            eprintln!("[vinowhisper-gui] could not save {}: {err}", path.display());
        }
    }

    pub fn save_to(&self, path: &Path) -> io::Result<()> {
        if let Some(dir) = path.parent() {
            std::fs::create_dir_all(dir)?;
        }
        let text = serde_json::to_string_pretty(self).map_err(io::Error::other)?;
        std::fs::write(path, text + "\n")
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

pub const DEFAULT_SHORTCUT: &str = "LOGO+ALT+C";

pub const DEFAULT_TRAY_IDLE_MINUTES: u64 = 30;

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct Settings {
    pub source: Source,
    pub position: Position,
    pub size: TextSize,
    pub shortcut: String,
    pub tray_idle_minutes: u64,
}

impl Default for Settings {
    fn default() -> Self {
        Settings {
            source: Source::default(),
            position: Position::default(),
            size: TextSize::default(),
            shortcut: DEFAULT_SHORTCUT.to_owned(),
            tray_idle_minutes: DEFAULT_TRAY_IDLE_MINUTES,
        }
    }
}

impl Settings {
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
            eprintln!("[vinowhisper-gui] ignoring {}: {err}", path.display());
            Settings::default()
        })
    }

    pub fn save(&self) {
        let path = Self::path();
        if let Err(err) = self.save_to(&path) {
            eprintln!("[vinowhisper-gui] could not save {}: {err}", path.display());
        }
    }

    pub fn save_to(&self, path: &Path) -> io::Result<()> {
        if let Some(dir) = path.parent() {
            std::fs::create_dir_all(dir)?;
        }
        let text = serde_json::to_string_pretty(self).map_err(io::Error::other)?;
        std::fs::write(path, text + "\n")
    }
}

pub fn home() -> PathBuf {
    std::env::var_os("HOME").map_or_else(|| PathBuf::from("/"), PathBuf::from)
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
            "vinowhisper-gui-settings-{}-{name}",
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
        };
        settings.save_to(&path).unwrap();
        assert_eq!(Settings::load_from(&path), settings);
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
    }

    #[test]
    fn the_tray_idle_time_is_read_from_the_file() {
        let path = scratch("tray-idle");
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(&path, r#"{"tray_idle_minutes": 0}"#).unwrap();
        assert_eq!(Settings::load_from(&path).tray_idle_minutes, 0);
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
    fn sources_are_named_the_way_the_caption_cli_takes_them() {
        assert_eq!(Source::parse("output"), Some(Source::Output));
        assert_eq!(Source::parse("mic"), Some(Source::Mic));
        assert_eq!(Source::parse("speakers"), None);
    }
}
