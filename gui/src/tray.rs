use std::sync::OnceLock;

use ksni::menu::{CheckmarkItem, RadioGroup, RadioItem, StandardItem, SubMenu};
use ksni::{Category, MenuItem, ToolTip};
use smithay_client_toolkit::reexports::calloop::channel::Sender;

use crate::APP_ID;
use crate::app::Command;
use crate::icon;
use crate::settings::{self, Language, Position, Source, Task, TextSize};
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
    pub passive: bool,
    pub autostart: bool,
    pub language: Language,
    pub task: Task,
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
    // Assumed, not checked: at login this can start before Plasma's tray does.
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

fn language_name(language: Language) -> &'static str {
    match language {
        Language::En => "English",
        Language::Fr => "French",
        Language::De => "German",
        Language::Es => "Spanish",
        Language::Auto => "Detect automatically",
    }
}

const SETUP_HINT: &str = "Not set up: run vinowhisper-setup --language auto";

fn language_menu(language: Language, task: Task, installed: bool) -> Vec<MenuItem<Tray>> {
    let mut items: Vec<MenuItem<Tray>> = vec![
        SubMenu {
            label: "Language".into(),
            submenu: vec![
                RadioGroup {
                    selected: Language::ALL
                        .iter()
                        .position(|option| *option == language)
                        .unwrap_or(0),
                    select: Box::new(|tray: &mut Tray, index| {
                        if let Some(option) = Language::ALL.get(index) {
                            tray.send(Command::SetLanguage(*option));
                        }
                    }),
                    options: Language::ALL
                        .iter()
                        .map(|option| RadioItem {
                            label: language_name(*option).into(),
                            // English needs no extra model.
                            enabled: installed || *option == Language::En,
                            ..Default::default()
                        })
                        .collect(),
                }
                .into(),
            ],
            ..Default::default()
        }
        .into(),
        CheckmarkItem {
            label: "Translate to English".into(),
            checked: task == Task::Translate,
            enabled: installed && language != Language::En,
            activate: Box::new(|tray: &mut Tray| {
                let translate = tray.view.task != Task::Translate;
                tray.send(Command::SetTranslate(translate));
            }),
            ..Default::default()
        }
        .into(),
    ];
    if !installed {
        items.push(
            StandardItem {
                label: SETUP_HINT.into(),
                enabled: false,
                ..Default::default()
            }
            .into(),
        );
    }
    items
}

fn shortcut_label(state: &ShortcutState) -> (String, bool) {
    match state {
        ShortcutState::Pending => ("Shortcut: asking the desktop…".into(), false),
        ShortcutState::Bound {
            trigger,
            configurable: true,
            ..
        } if trigger.is_empty() => ("Set a keyboard shortcut…".into(), true),
        ShortcutState::Bound {
            trigger,
            configurable: true,
            ..
        } => (format!("Change shortcut ({trigger})…"), true),
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

fn tray_status(passive: bool) -> ksni::Status {
    if passive {
        ksni::Status::Passive
    } else {
        ksni::Status::Active
    }
}

fn dictate_label(state: &ShortcutState) -> String {
    match state {
        ShortcutState::Bound { dictate, .. } if !dictate.is_empty() => {
            format!("Dictate ({dictate}): hold to talk, or tap to start and stop")
        }
        _ => "Dictate: no key bound (or bind “vinowhisper-gui dictate”)".into(),
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

    fn status(&self) -> ksni::Status {
        tray_status(self.view.passive)
    }

    fn icon_name(&self) -> String {
        format!("{APP_ID}-symbolic")
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

    fn activate(&mut self, _x: i32, _y: i32) {
        self.send(Command::Toggle);
    }

    fn menu(&self) -> Vec<MenuItem<Self>> {
        let (shortcut, can_configure) = shortcut_label(&self.view.shortcut);
        // At menu-open, not in View: running vinowhisper-setup needs no tray restart.
        let language = language_menu(
            self.view.language,
            self.view.task,
            settings::multilingual_installed(),
        );
        let mut menu = vec![
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
        ];
        menu.extend(language);
        menu.push(MenuItem::Separator);
        menu.extend([
            StandardItem {
                label: dictate_label(&self.view.shortcut),
                enabled: false,
                ..Default::default()
            }
            .into(),
            StandardItem {
                label: shortcut,
                enabled: can_configure,
                activate: Box::new(|tray: &mut Self| tray.send(Command::ConfigureShortcut)),
                ..Default::default()
            }
            .into(),
            CheckmarkItem {
                label: "Start at login".into(),
                checked: self.view.autostart,
                activate: Box::new(|tray: &mut Self| {
                    tray.send(Command::SetAutostart(!tray.view.autostart))
                }),
                ..Default::default()
            }
            .into(),
            StandardItem {
                label: "Quit".into(),
                activate: Box::new(|tray: &mut Self| tray.send(Command::Quit)),
                ..Default::default()
            }
            .into(),
        ]);
        menu
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn bound(trigger: &str, configurable: bool) -> ShortcutState {
        ShortcutState::Bound {
            trigger: trigger.into(),
            dictate: String::new(),
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
    fn the_dictation_key_is_named_or_the_fallback_command_is() {
        let bound = ShortcutState::Bound {
            trigger: "Meta+Alt+C".into(),
            dictate: "Meta+H".into(),
            configurable: true,
        };
        assert!(dictate_label(&bound).contains("Meta+H"));
        assert!(dictate_label(&ShortcutState::Pending).contains("vinowhisper-gui dictate"));
    }

    fn labels(items: &[MenuItem<Tray>]) -> Vec<String> {
        items
            .iter()
            .filter_map(|item| match item {
                MenuItem::Standard(item) => Some(item.label.clone()),
                MenuItem::Checkmark(item) => Some(item.label.clone()),
                MenuItem::SubMenu(item) => Some(item.label.clone()),
                _ => None,
            })
            .collect()
    }

    #[test]
    fn without_the_multilingual_export_the_tray_says_what_to_run() {
        let items = language_menu(Language::En, Task::Transcribe, false);
        assert!(labels(&items).contains(&SETUP_HINT.to_owned()));
        let MenuItem::Checkmark(translate) = &items[1] else {
            panic!("second item is the translate checkmark");
        };
        assert!(!translate.enabled);
    }

    #[test]
    fn translate_needs_a_foreign_language_and_the_export() {
        let enabled = |language, installed| {
            let items = language_menu(language, Task::Transcribe, installed);
            let MenuItem::Checkmark(translate) = &items[1] else {
                panic!("second item is the translate checkmark");
            };
            translate.enabled
        };
        assert!(enabled(Language::Fr, true));
        assert!(enabled(Language::Auto, true));
        assert!(!enabled(Language::En, true));
        assert!(!enabled(Language::Fr, false));
    }

    #[test]
    fn an_installed_export_drops_the_hint() {
        let items = language_menu(Language::Fr, Task::Translate, true);
        assert!(!labels(&items).contains(&SETUP_HINT.to_owned()));
    }

    #[test]
    fn an_idle_overlay_asks_to_be_tucked_away() {
        assert_eq!(tray_status(true), ksni::Status::Passive);
        assert_eq!(tray_status(false), ksni::Status::Active);
    }

    #[test]
    fn menu_labels_carry_no_stray_access_key_underscores() {
        // ksni treats "_" as an access-key marker and hides it.
        let names = Source::ALL
            .map(source_name)
            .into_iter()
            .chain(Position::ALL.map(position_name))
            .chain(TextSize::ALL.map(size_name))
            .chain(Language::ALL.map(language_name))
            .chain([SETUP_HINT]);
        for name in names {
            assert!(!name.contains('_'), "{name}");
        }
    }
}
