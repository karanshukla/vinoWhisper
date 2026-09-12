//! The tray icon: a StatusNotifierItem over D-Bus, which is what Plasma's
//! system tray speaks natively (and GNOME's, with the AppIndicator extension).
//!
//! It holds a copy of what its menu shows and decides nothing. Every choice
//! made in it goes to the main loop as a Command, and the main loop answers
//! with a fresh View.

use std::sync::OnceLock;

use ksni::menu::{CheckmarkItem, RadioGroup, RadioItem, StandardItem, SubMenu};
use ksni::{Category, MenuItem, ToolTip};
use smithay_client_toolkit::reexports::calloop::channel::Sender;

use crate::app::Command;
use crate::icon;
use crate::settings::{Position, Source, TextSize};
use crate::shortcut::State as ShortcutState;

pub type Handle = ksni::blocking::Handle<Tray>;

#[derive(Debug, Clone, PartialEq)]
pub struct View {
    pub visible: bool,
    pub source: Source,
    pub position: Position,
    pub size: TextSize,
    pub status: String,
    pub shortcut: ShortcutState,
}

pub struct Tray {
    tx: Sender<Command>,
    view: View,
}

impl Tray {
    pub fn new(tx: Sender<Command>, view: View) -> Tray {
        Tray { tx, view }
    }

    fn send(&self, command: Command) {
        let _ = self.tx.send(command);
    }
}

pub fn spawn(tray: Tray) -> Option<Handle> {
    use ksni::blocking::TrayMethods;
    // Assumed rather than checked: at login this can start before Plasma's
    // tray does, and an icon that registers a moment late beats none.
    match tray.assume_sni_available(true).spawn() {
        Ok(handle) => Some(handle),
        Err(err) => {
            eprintln!(
                "[vinowhisper-gui] no tray icon ({err}); the overlay and the shortcut still work"
            );
            None
        }
    }
}

pub fn show(handle: &Handle, view: View) {
    handle.update(move |tray| tray.view = view);
}

fn source_name(source: Source) -> &'static str {
    match source {
        Source::Output => "System audio",
        Source::Mic => "Microphone",
    }
}

fn position_name(position: Position) -> &'static str {
    match position {
        Position::Bottom => "Bottom of the screen",
        Position::Top => "Top of the screen",
    }
}

fn size_name(size: TextSize) -> &'static str {
    match size {
        TextSize::Small => "Small",
        TextSize::Medium => "Medium",
        TextSize::Large => "Large",
    }
}

/// The shortcut entry's label, and whether choosing it can do anything.
fn shortcut_label(state: &ShortcutState) -> (String, bool) {
    match state {
        ShortcutState::Pending => ("Shortcut: asking the desktop…".into(), false),
        ShortcutState::Bound {
            trigger,
            configurable: true,
        } if trigger.is_empty() => ("Set a keyboard shortcut…".into(), true),
        ShortcutState::Bound {
            trigger,
            configurable: true,
        } => (format!("Change shortcut ({trigger})…"), true),
        // Portal version 1 cannot open its own settings page, but the binding
        // is still an ordinary desktop shortcut and can be changed there.
        ShortcutState::Bound { trigger, .. } if trigger.is_empty() => (
            "No shortcut bound: set one in System Settings".into(),
            false,
        ),
        ShortcutState::Bound { trigger, .. } => (
            format!("Shortcut: {trigger} (change it in System Settings)"),
            false,
        ),
        ShortcutState::Unavailable(_) => (
            "No global shortcut here: bind “vinowhisper-gui toggle” instead".into(),
            false,
        ),
    }
}

fn radio_menu<T: Copy + PartialEq + Send + Sync + 'static>(
    label: &str,
    options: &'static [T],
    selected: T,
    name: fn(T) -> &'static str,
    command: fn(T) -> Command,
) -> MenuItem<Tray> {
    SubMenu {
        label: label.into(),
        submenu: vec![
            RadioGroup {
                selected: options
                    .iter()
                    .position(|option| *option == selected)
                    .unwrap_or(0),
                select: Box::new(move |tray: &mut Tray, index| {
                    if let Some(option) = options.get(index) {
                        tray.send(command(*option));
                    }
                }),
                options: options
                    .iter()
                    .map(|option| RadioItem {
                        label: name(*option).into(),
                        ..Default::default()
                    })
                    .collect(),
            }
            .into(),
        ],
        ..Default::default()
    }
    .into()
}

impl ksni::Tray for Tray {
    fn id(&self) -> String {
        "vinowhisper".into()
    }

    fn title(&self) -> String {
        "vinoWhisper".into()
    }

    fn category(&self) -> Category {
        Category::ApplicationStatus
    }

    fn icon_name(&self) -> String {
        // Themed and symbolic, so Plasma recolours it to match the panel.
        "media-view-subtitles-symbolic".into()
    }

    fn icon_pixmap(&self) -> Vec<ksni::Icon> {
        static ICONS: OnceLock<Vec<ksni::Icon>> = OnceLock::new();
        ICONS.get_or_init(icon::tray_icons).clone()
    }

    fn tool_tip(&self) -> ToolTip {
        ToolTip {
            title: "vinoWhisper".into(),
            description: self.view.status.clone(),
            ..Default::default()
        }
    }

    /// A left click does what the shortcut does.
    fn activate(&mut self, _x: i32, _y: i32) {
        self.send(Command::Toggle);
    }

    fn menu(&self) -> Vec<MenuItem<Self>> {
        let (shortcut, can_configure) = shortcut_label(&self.view.shortcut);
        vec![
            CheckmarkItem {
                label: "Show captions".into(),
                checked: self.view.visible,
                activate: Box::new(|tray: &mut Self| {
                    tray.send(if tray.view.visible {
                        Command::Hide
                    } else {
                        Command::Show
                    })
                }),
                ..Default::default()
            }
            .into(),
            MenuItem::Separator,
            radio_menu(
                "Listen to",
                &Source::ALL,
                self.view.source,
                source_name,
                Command::SetSource,
            ),
            radio_menu(
                "Position",
                &Position::ALL,
                self.view.position,
                position_name,
                Command::SetPosition,
            ),
            radio_menu(
                "Text size",
                &TextSize::ALL,
                self.view.size,
                size_name,
                Command::SetSize,
            ),
            MenuItem::Separator,
            StandardItem {
                label: shortcut,
                enabled: can_configure,
                icon_name: "preferences-desktop-keyboard-shortcuts".into(),
                activate: Box::new(|tray: &mut Self| tray.send(Command::ConfigureShortcut)),
                ..Default::default()
            }
            .into(),
            StandardItem {
                label: "Quit".into(),
                icon_name: "application-exit".into(),
                activate: Box::new(|tray: &mut Self| tray.send(Command::Quit)),
                ..Default::default()
            }
            .into(),
        ]
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn bound(trigger: &str, configurable: bool) -> ShortcutState {
        ShortcutState::Bound {
            trigger: trigger.into(),
            configurable,
        }
    }

    #[test]
    fn a_bound_shortcut_shows_its_trigger_and_can_be_changed() {
        assert_eq!(
            shortcut_label(&bound("Meta+Alt+C", true)),
            ("Change shortcut (Meta+Alt+C)…".to_owned(), true)
        );
    }

    #[test]
    fn a_declined_shortcut_can_still_be_set_later() {
        assert_eq!(
            shortcut_label(&bound("", true)),
            ("Set a keyboard shortcut…".to_owned(), true)
        );
    }

    #[test]
    fn an_old_portal_points_at_system_settings_instead() {
        let (label, enabled) = shortcut_label(&bound("Meta+Alt+C", false));
        assert!(label.contains("System Settings"));
        assert!(!enabled);
    }

    #[test]
    fn no_portal_names_the_command_to_bind() {
        let (label, enabled) = shortcut_label(&ShortcutState::Unavailable("no portal".into()));
        assert!(label.contains("vinowhisper-gui toggle"));
        assert!(!enabled);
    }

    #[test]
    fn menu_labels_carry_no_stray_access_key_underscores() {
        // ksni treats "_" as an access-key marker and hides it.
        let names = Source::ALL
            .map(source_name)
            .into_iter()
            .chain(Position::ALL.map(position_name))
            .chain(TextSize::ALL.map(size_name));
        for name in names {
            assert!(!name.contains('_'), "{name}");
        }
    }
}
