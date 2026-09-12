//! What the tray menu changes, remembered between runs.
//!
//! `$XDG_CONFIG_HOME/vinowhisper/gui.json`. Every field has a default and an
//! unreadable file is reported and ignored, because a settings file is never
//! a reason for captions not to start.

use std::io;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Source {
    /// System audio, via the default sink's monitor. What this was built for.
    #[default]
    Output,
    Mic,
}

impl Source {
    pub const ALL: [Source; 2] = [Source::Output, Source::Mic];

    /// The value `vinowhisper-caption --source` takes.
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
    /// For video that burns its own subtitles into the bottom of the frame.
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

    /// Caption font size in logical pixels.
    pub fn caption_px(self) -> f32 {
        match self {
            TextSize::Small => 20.0,
            TextSize::Medium => 26.0,
            TextSize::Large => 34.0,
        }
    }
}

/// In the XDG shortcuts syntax the portal takes, and only ever a *preferred*
/// trigger: the desktop decides, Plasma asks the user first, and once bound
/// the shortcut lives in the desktop's own settings, where it is changed like
/// any other. Changing this later has no effect on a binding that already
/// exists; use "Change shortcut…" in the tray menu for that.
///
/// Meta+Alt+C rather than the Meta+H once planned for this: Meta+H is bound
/// to Ghostty's new-window on the machine this was built on.
pub const DEFAULT_SHORTCUT: &str = "LOGO+ALT+C";

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct Settings {
    pub source: Source,
    pub position: Position,
    pub size: TextSize,
    pub shortcut: String,
}

impl Default for Settings {
    fn default() -> Self {
        Settings {
            source: Source::default(),
            position: Position::default(),
            size: TextSize::default(),
            shortcut: DEFAULT_SHORTCUT.to_owned(),
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
    }

    #[test]
    fn a_corrupt_file_does_not_stop_captions() {
        let path = scratch("corrupt");
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(&path, "{not json").unwrap();
        assert_eq!(Settings::load_from(&path), Settings::default());
    }

    #[test]
    fn sources_are_named_the_way_the_caption_cli_takes_them() {
        assert_eq!(Source::parse("output"), Some(Source::Output));
        assert_eq!(Source::parse("mic"), Some(Source::Mic));
        assert_eq!(Source::parse("speakers"), None);
    }
}
