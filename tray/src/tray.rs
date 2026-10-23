use std::sync::OnceLock;

use ksni::menu::{CheckmarkItem, RadioGroup, RadioItem, StandardItem, SubMenu};
use ksni::{Category, MenuItem, ToolTip};
use smithay_client_toolkit::reexports::calloop::channel::Sender;

use crate::APP_ID;
use crate::app::Command;
use crate::captions::Phase;
use crate::dictation::Recent;
use crate::icon;
use crate::settings::Source;
use crate::shortcut::State as ShortcutState;

pub type Handle = ksni::blocking::Handle<Tray>;

#[derive(Debug, Clone, PartialEq)]
pub struct View {
    pub visible: bool,
    pub source: Source,
    pub status: String,
    pub dictation: Option<String>,
    pub shortcut: ShortcutState,
    pub passive: bool,
    pub autostart: bool,
    pub recent: Recent,
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
                "[vinowhisper-tray] no tray icon ({err}); the overlay and the shortcut still work"
            );
            None
        }
    }
}

pub fn show(handle: &Handle, view: View) {
    handle.update(move |tray| tray.view = view);
}

pub fn caption_status(
    visible: bool,
    phase: &Phase,
    device: Option<&str>,
    degraded: bool,
) -> String {
    if !visible {
        return "Captions hidden".into();
    }
    match phase {
        Phase::Starting => "Captions starting…".into(),
        Phase::Live => match (device, degraded) {
            (Some(device), true) => {
                format!("Captions live on {device}, not the NPU: expect more lag")
            }
            (Some(device), false) => format!("Captions live on {device}"),
            (None, _) => "Captions live".into(),
        },
        Phase::Stopped => "Captions stopped".into(),
        Phase::Failed(message) => format!("Captions stopped: {message}"),
    }
}

// The icon never changes, so this is where every state has to show, in words.
fn tool_tip_text(view: &View) -> String {
    match &view.dictation {
        Some(dictation) => format!("{dictation}\n{}", view.status),
        None => view.status.clone(),
    }
}

fn source_name(source: Source) -> &'static str {
    match source {
        Source::Output => "System audio",
        Source::Mic => "Microphone",
    }
}

const LABEL_CHARS: usize = 40;

fn recent_label(text: &str) -> String {
    let flat = text.split_whitespace().collect::<Vec<_>>().join(" ");
    let shown = if flat.chars().count() > LABEL_CHARS {
        let head: String = flat.chars().take(LABEL_CHARS - 1).collect();
        format!("{}…", head.trim_end())
    } else {
        flat
    };
    // ksni takes "_" as an access-key marker; a doubled one is a literal underscore.
    shown.replace('_', "__")
}

fn recent_menu(recent: &Recent) -> MenuItem<Tray> {
    let mut submenu: Vec<MenuItem<Tray>> = recent
        .items()
        .map(|(id, text)| {
            StandardItem {
                label: recent_label(text),
                activate: Box::new(move |tray: &mut Tray| tray.send(Command::CopyRecent(id))),
                ..Default::default()
            }
            .into()
        })
        .collect();
    if recent.is_empty() {
        submenu.push(
            StandardItem {
                label: "None yet".into(),
                enabled: false,
                ..Default::default()
            }
            .into(),
        );
    } else {
        submenu.push(MenuItem::Separator);
        submenu.push(
            StandardItem {
                label: "Clear".into(),
                activate: Box::new(|tray: &mut Tray| tray.send(Command::ClearRecent)),
                ..Default::default()
            }
            .into(),
        );
    }
    SubMenu {
        label: "Recent dictations".into(),
        submenu,
        ..Default::default()
    }
    .into()
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
        _ => "Dictate: no key bound (see Settings, Shortcuts)".into(),
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
            description: tool_tip_text(&self.view),
            ..Default::default()
        }
    }

    fn activate(&mut self, _x: i32, _y: i32) {
        self.send(Command::Toggle);
    }

    fn menu(&self) -> Vec<MenuItem<Self>> {
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
            radio_menu(
                "Listen to",
                &Source::ALL,
                self.view.source,
                source_name,
                Command::SetSource,
            ),
            MenuItem::Separator,
            recent_menu(&self.view.recent),
            StandardItem {
                label: dictate_label(&self.view.shortcut),
                enabled: false,
                ..Default::default()
            }
            .into(),
            MenuItem::Separator,
            StandardItem {
                label: "Settings…".into(),
                icon_name: "configure".into(),
                activate: Box::new(|tray: &mut Self| tray.send(Command::OpenSettings)),
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
        ]
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_dictation_key_is_named_or_the_fallback_command_is() {
        let bound = ShortcutState::Bound {
            trigger: "Meta+Alt+C".into(),
            dictate: "Meta+H".into(),
            configurable: true,
        };
        assert!(dictate_label(&bound).contains("Meta+H"));
        assert!(dictate_label(&ShortcutState::Pending).contains("Settings"));
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

    use ksni::Tray as _;
    use smithay_client_toolkit::reexports::calloop::channel;

    fn view() -> View {
        View {
            visible: true,
            source: Source::Output,
            status: caption_status(true, &Phase::Live, Some("NPU"), false),
            dictation: None,
            shortcut: ShortcutState::Pending,
            passive: false,
            autostart: false,
            recent: Recent::default(),
        }
    }

    fn tray(view: View) -> Tray {
        let (tx, _rx) = channel::channel();
        Tray::new(tx, view)
    }

    // Every item a screen reader can land on, submenus and radio options included.
    fn all_labels(items: &[MenuItem<Tray>], out: &mut Vec<String>) {
        for item in items {
            match item {
                MenuItem::Standard(item) => out.push(item.label.clone()),
                MenuItem::Checkmark(item) => out.push(item.label.clone()),
                MenuItem::SubMenu(item) => {
                    out.push(item.label.clone());
                    all_labels(&item.submenu, out);
                }
                MenuItem::RadioGroup(group) => {
                    out.extend(group.options.iter().map(|option| option.label.clone()))
                }
                MenuItem::Separator => {}
            }
        }
    }

    #[test]
    fn every_menu_item_has_a_text_label() {
        let mut recent = Recent::default();
        recent.push("hello");
        for view in [
            view(),
            View {
                recent,
                visible: false,
                ..view()
            },
        ] {
            let mut found = Vec::new();
            all_labels(&tray(view).menu(), &mut found);
            assert!(found.len() >= 9, "{found:?}");
            for label in &found {
                assert!(!label.trim().is_empty(), "{found:?}");
            }
        }
    }

    #[test]
    fn on_off_items_are_checkmarks_that_report_their_state() {
        for (visible, autostart) in [(true, false), (false, true)] {
            let menu = tray(View {
                visible,
                autostart,
                ..view()
            })
            .menu();
            let checks: Vec<(String, bool)> = menu
                .iter()
                .filter_map(|item| match item {
                    MenuItem::Checkmark(item) => Some((item.label.clone(), item.checked)),
                    _ => None,
                })
                .collect();
            assert_eq!(
                checks,
                [
                    ("Show captions".to_owned(), visible),
                    ("Start at login".to_owned(), autostart)
                ]
            );
        }
    }

    #[test]
    fn the_source_is_a_radio_group_with_the_current_one_selected() {
        for source in Source::ALL {
            let menu = tray(View { source, ..view() }).menu();
            let listen = menu
                .iter()
                .find_map(|item| match item {
                    MenuItem::SubMenu(menu) if menu.label == "Listen to" => Some(menu),
                    _ => None,
                })
                .expect("a Listen to submenu");
            let MenuItem::RadioGroup(group) = &listen.submenu[0] else {
                panic!("a radio group");
            };
            assert_eq!(group.options[group.selected].label, source_name(source));
        }
    }

    #[test]
    fn the_item_is_named_and_every_state_reads_differently() {
        let views = [
            caption_status(false, &Phase::Live, Some("NPU"), false),
            caption_status(true, &Phase::Starting, None, false),
            caption_status(true, &Phase::Live, Some("NPU"), false),
            caption_status(true, &Phase::Live, Some("CPU"), true),
            caption_status(true, &Phase::Stopped, None, false),
            caption_status(true, &Phase::Failed("no audio".into()), None, false),
        ]
        .into_iter()
        .flat_map(|status| {
            [
                None,
                Some("Dictation recording (release the key to finish)".to_owned()),
                Some("Dictation failed: the server stopped".to_owned()),
            ]
            .map(|dictation| View {
                status: status.clone(),
                dictation,
                ..view()
            })
        });
        let mut seen = std::collections::HashSet::new();
        for view in views {
            let tray = tray(view);
            assert_eq!(tray.title(), "vinoWhisper");
            let tip = tray.tool_tip();
            assert_eq!(tip.title, "vinoWhisper");
            assert!(seen.insert(tip.description.clone()), "{}", tip.description);
        }
        assert_eq!(seen.len(), 18);
        assert!(seen.iter().any(|tip| tip.contains("not the NPU")));
        assert!(
            seen.iter()
                .any(|tip| tip.contains("Captions stopped: no audio"))
        );
        assert!(
            seen.iter()
                .any(|tip| tip.starts_with("Dictation recording"))
        );
    }

    #[test]
    fn an_idle_overlay_asks_to_be_tucked_away() {
        assert_eq!(tray_status(true), ksni::Status::Passive);
        assert_eq!(tray_status(false), ksni::Status::Active);
    }

    #[test]
    fn menu_labels_carry_no_stray_access_key_underscores() {
        // ksni treats "_" as an access-key marker and hides it.
        let names = Source::ALL.map(source_name);
        for name in names {
            assert!(!name.contains('_'), "{name}");
        }
    }

    fn submenu_labels(item: &MenuItem<Tray>) -> Vec<String> {
        let MenuItem::SubMenu(menu) = item else {
            panic!("a submenu");
        };
        labels(&menu.submenu)
    }

    #[test]
    fn an_empty_history_says_none_yet_and_offers_no_clear() {
        let item = recent_menu(&Recent::default());
        assert_eq!(submenu_labels(&item), ["None yet"]);
        let MenuItem::SubMenu(menu) = &item else {
            panic!("a submenu");
        };
        let MenuItem::Standard(none) = &menu.submenu[0] else {
            panic!("a standard item");
        };
        assert!(!none.enabled);
    }

    #[test]
    fn the_history_lists_newest_first_with_clear_last() {
        let mut recent = Recent::default();
        recent.push("first");
        recent.push("second");
        assert_eq!(
            submenu_labels(&recent_menu(&recent)),
            ["second", "first", "Clear"]
        );
    }

    #[test]
    fn long_labels_are_cut_and_flattened() {
        let text = format!("one\ntwo {}", "word ".repeat(30));
        let label = recent_label(&text);
        assert!(label.starts_with("one two word"));
        assert!(label.ends_with('…'));
        assert!(label.chars().count() <= LABEL_CHARS);
        assert_eq!(recent_label("short"), "short");
    }

    #[test]
    fn underscores_in_a_label_are_escaped() {
        assert_eq!(recent_label("snake_case"), "snake__case");
    }
}
