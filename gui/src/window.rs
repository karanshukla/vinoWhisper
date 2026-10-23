use std::process::ExitCode;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use eframe::egui::accesskit::{Live, Role};
use eframe::egui::{self, Align, Color32, FontId, Layout, RichText, Sense, Stroke};

use crate::forward::{self, Shortcuts};
use crate::secret::{self, Key};
use crate::theme;
use vinowhisper_settings::{
    Cleanup, CleanupMode, Language, Position, Settings, Source, Speech, Task, TextSize,
    multilingual_installed,
};

#[derive(Clone, Copy, PartialEq, Eq)]
enum Page {
    Shortcuts,
    Captions,
    Speech,
    Cleanup,
}

impl Page {
    const ALL: [Page; 4] = [Page::Shortcuts, Page::Captions, Page::Speech, Page::Cleanup];

    fn parse(name: &str) -> Option<Page> {
        Page::ALL
            .into_iter()
            .find(|page| page.title().eq_ignore_ascii_case(name))
    }

    fn beta(self) -> bool {
        self == Page::Cleanup
    }

    fn title(self) -> &'static str {
        match self {
            Page::Shortcuts => "Shortcuts",
            Page::Captions => "Captions",
            Page::Speech => "Speech",
            Page::Cleanup => "Cleanup",
        }
    }
}

fn source_label(source: Source) -> &'static str {
    match source {
        Source::Output => "What is playing (speakers)",
        Source::Mic => "Microphone",
    }
}

fn position_label(position: Position) -> &'static str {
    match position {
        Position::Bottom => "Bottom of the screen",
        Position::Top => "Top of the screen",
    }
}

fn size_label(size: TextSize) -> &'static str {
    match size {
        TextSize::Small => "Small",
        TextSize::Medium => "Medium",
        TextSize::Large => "Large",
    }
}

fn mode_label(mode: CleanupMode) -> &'static str {
    match mode {
        CleanupMode::Smart => "Only when it hears something to fix",
        CleanupMode::Always => "Every dictation",
    }
}

fn language_label(language: Language) -> &'static str {
    match language {
        Language::En => "English",
        Language::Fr => "French",
        Language::De => "German",
        Language::Es => "Spanish",
        Language::Auto => "Detect automatically",
    }
}

const COMBO_WIDTH: f32 = 240.0;

fn labelled(response: egui::Response, label: Option<egui::Id>) -> egui::Response {
    match label {
        Some(id) => response.labelled_by(id),
        None => response,
    }
}

fn combo<T: Copy + PartialEq>(
    ui: &mut egui::Ui,
    id: &str,
    name: Option<egui::Id>,
    value: &mut T,
    all: &[T],
    label: fn(T) -> &'static str,
) -> bool {
    let before = *value;
    let shown = egui::ComboBox::from_id_salt(id)
        // Never past the edge: at the largest text the form column is narrower than this,
        // and an unwrapped choice pushed the whole column out with it.
        .width(COMBO_WIDTH.min(ui.available_width()))
        .wrap()
        .selected_text(label(*value))
        .show_ui(ui, |ui| {
            for &option in all {
                ui.selectable_value(value, option, label(option));
            }
        });
    labelled(shown.response, name);
    *value != before
}

fn number<N: egui::emath::Numeric>(
    ui: &mut egui::Ui,
    name: Option<egui::Id>,
    value: &mut N,
    range: std::ops::RangeInclusive<N>,
    suffix: &str,
) -> bool {
    let response = ui.add(egui::DragValue::new(value).range(range).suffix(suffix));
    labelled(response, name)
        // DragValue's own hover cursor is a horizontal resize arrow, which reads as a splitter here.
        .on_hover_cursor(egui::CursorIcon::Text)
        .changed()
}

// Below this the sidebar folds into tabs in the header, and form labels go above their control.
const SIDEBAR_FROM: f32 = 620.0;
const SIDEBAR_WIDTH: f32 = 180.0;
const LABEL_WIDTH: f32 = 150.0;
const FORM_MAX_WIDTH: f32 = 620.0;
// Between the form and the scroll bar, which sits on the window's right edge.
const SCROLL_GAP: f32 = 24.0;
const TWO_COLUMNS_FROM: f32 = 440.0;

struct SettingsWindow {
    page: Page,
    settings: Settings,
    speech: Speech,
    cleanup: Cleanup,
    cleanup_dirty: bool,
    key: Arc<Mutex<Key>>,
    key_input: String,
    multilingual: bool,
    focused: bool,
    kde_header: Option<theme::KdeHeader>,
    // The last factor read from the desktop: zoom follows it only when it changes, so a
    // Ctrl + or - since then is kept.
    text_scale: Option<f32>,
    shortcuts: Arc<Mutex<Shortcuts>>,
    // Which page last asked the tray, so arriving on Shortcuts asks again.
    shown: Option<Page>,
    refused: Arc<Mutex<Option<String>>>,
    notice: Option<String>,
}

impl SettingsWindow {
    fn new(page: Option<Page>) -> Self {
        let mut window = Self::with(page, Settings::load(), Speech::load(), Cleanup::load());
        window.multilingual = multilingual_installed();
        window.kde_header = theme::kde_header();
        window
    }

    fn follow_text_scale(&mut self, ctx: &egui::Context, scale: Option<f32>) {
        if scale != self.text_scale {
            self.text_scale = scale;
            ctx.set_zoom_factor(scale.unwrap_or(1.0));
        }
    }

    fn with(page: Option<Page>, settings: Settings, speech: Speech, cleanup: Cleanup) -> Self {
        SettingsWindow {
            page: page.unwrap_or(Page::Shortcuts),
            settings,
            speech,
            cleanup,
            cleanup_dirty: false,
            key: Arc::new(Mutex::new(Key::Checking)),
            key_input: String::new(),
            multilingual: false,
            focused: true,
            kde_header: None,
            text_scale: None,
            shortcuts: Arc::new(Mutex::new(Shortcuts::Asking)),
            shown: None,
            refused: Arc::new(Mutex::new(None)),
            notice: None,
        }
    }

    fn ask_shortcuts(&self, ctx: &egui::Context, after: Duration) {
        let shortcuts = Arc::clone(&self.shortcuts);
        let ctx = ctx.clone();
        std::thread::spawn(move || {
            std::thread::sleep(after);
            let answer = forward::query_shortcuts();
            *shortcuts.lock().unwrap() = answer;
            ctx.request_repaint();
        });
    }

    fn shortcuts(&mut self, ui: &mut egui::Ui) {
        let p = theme::palette(ui.ctx());
        let state = self.shortcuts.lock().unwrap().clone();
        section(ui, "Global shortcuts");
        match state {
            Shortcuts::Asking | Shortcuts::Pending => {
                row(ui, "", "", |ui, _| {
                    ui.label("Asking the desktop…");
                });
            }
            Shortcuts::Bound {
                captions,
                dictate,
                configurable,
            } => {
                row(
                    ui,
                    "Captions:",
                    "Shows or hides the floating captions.",
                    |ui, _| keys(ui, &captions),
                );
                row(
                    ui,
                    "Dictation:",
                    "Hold to talk and release to type, or tap to start and tap again to finish.",
                    |ui, _| keys(ui, &dictate),
                );
                if configurable {
                    row(
                        ui,
                        "",
                        "Opens your desktop's shortcut settings.",
                        |ui, _| {
                            if ui.button("Change…").clicked() {
                                forward::run_tray(&["configure-shortcut"]);
                            }
                        },
                    );
                } else {
                    row(
                        ui,
                        "",
                        "Change them in your desktop's shortcut settings, where they are listed under vinoWhisper.",
                        |_, _| {},
                    );
                }
            }
            Shortcuts::Stopped => {
                row(
                    ui,
                    "",
                    "vinoWhisper is not running, so no shortcut is active.",
                    |ui, _| {
                        if ui.button("Start vinoWhisper").clicked() {
                            forward::run_tray(&["--hidden"]);
                            // The portal takes a moment to answer a fresh tray.
                            self.ask_shortcuts(ui.ctx(), Duration::from_millis(1500));
                        }
                    },
                );
            }
            Shortcuts::Unavailable => {
                ui.label(
                    RichText::new(
                        "This desktop has no global shortcuts portal. Bind these commands to keys in its keyboard settings instead:",
                    )
                    .color(p.muted),
                );
                ui.add_space(6.0);
                row(ui, "Captions:", "", |ui, _| {
                    command(ui, "vinowhisper-tray toggle");
                });
                row(
                    ui,
                    "Dictation:",
                    "On the key's press and release, for hold to talk. Or vinowhisper-tray dictate on one key, to tap.",
                    |ui, _| {
                        command(ui, "vinowhisper-tray dictate-press");
                        command(ui, "vinowhisper-tray dictate-release");
                    },
                );
            }
            Shortcuts::Unknown(reason) => {
                row(ui, "", "", |ui, _| {
                    ui.label(RichText::new(reason).color(p.muted));
                });
            }
        }
    }

    fn captions(&mut self, ui: &mut egui::Ui) -> bool {
        let mut changed = false;
        section(ui, "Overlay");
        row(
            ui,
            "Position:",
            "Where the caption box sits, above other windows.",
            |ui, label| {
                changed |= combo(
                    ui,
                    "position",
                    label,
                    &mut self.settings.position,
                    &Position::ALL,
                    position_label,
                );
            },
        );
        row(ui, "Text size:", "", |ui, label| {
            changed |= combo(
                ui,
                "size",
                label,
                &mut self.settings.size,
                &TextSize::ALL,
                size_label,
            );
        });
        section(ui, "Listening");
        row(
            ui,
            "Listen to:",
            "What the captions follow.",
            |ui, label| {
                changed |= combo(
                    ui,
                    "source",
                    label,
                    &mut self.settings.source,
                    &Source::ALL,
                    source_label,
                );
            },
        );
        row(
            ui,
            "Hide after silence:",
            "Seconds with no audio before the captions count down and hide. 0 keeps them up.",
            |ui, label| {
                changed |= number(
                    ui,
                    label,
                    &mut self.settings.hide_after_silence_s,
                    0..=3600,
                    " s",
                );
            },
        );
        row(
            ui,
            "Transcripts:",
            "Write each session to a file you can read back later.",
            |ui, _| {
                changed |= ui
                    .checkbox(&mut self.settings.save_transcripts, "Save transcripts")
                    .changed();
            },
        );
        section(ui, "Tray");
        row(
            ui,
            "Unload after:",
            "Idle time before the tray icon tucks itself away. 0 keeps it up.",
            |ui, label| {
                changed |= number(
                    ui,
                    label,
                    &mut self.settings.tray_idle_minutes,
                    0..=1440,
                    " min",
                );
            },
        );
        changed
    }

    fn run_key(&self, ctx: &egui::Context, job: impl FnOnce() -> Key + Send + 'static) {
        let key = Arc::clone(&self.key);
        let ctx = ctx.clone();
        *key.lock().unwrap() = Key::Checking;
        std::thread::spawn(move || {
            let answer = job();
            *key.lock().unwrap() = answer;
            ctx.request_repaint();
        });
    }

    fn cleanup(&mut self, ui: &mut egui::Ui) {
        let p = theme::palette(ui.ctx());
        ui.add_space(10.0);
        egui::Frame::NONE
            .fill(p.code_bg)
            .stroke(Stroke::new(1.0_f32, p.rule))
            .corner_radius(4.0)
            .inner_margin(egui::Margin::symmetric(10, 8))
            .show(ui, |ui| {
                ui.label(RichText::new("Beta").family(theme::bold()));
                ui.add(
                    egui::Label::new(
                        RichText::new(
                            "New, and tuned on one microphone. It may skip a quiet word, or leave \
                             text as you said it when the model's answer does not check out. Tell us \
                             what it gets wrong.",
                        )
                        .size(12.5)
                        .color(p.muted),
                    )
                    .wrap(),
                );
            });
        section(ui, "Dictation cleanup");
        row(
            ui,
            "Clean up:",
            "Sends what you dictate to a language model before it is typed, to drop ums, apply \
             corrections and act on \"scratch that\". It only ever deletes words, and any other \
             answer is thrown away and the raw text is typed. Off by default.",
            |ui, _| {
                self.cleanup_dirty |= ui
                    .checkbox(&mut self.cleanup.enabled, "Clean up dictation")
                    .changed();
            },
        );
        let off = !self.cleanup.enabled;
        ui.add_enabled_ui(!off, |ui| {
            section(ui, "Endpoint");
            let wide = (ui.available_width() - LABEL_WIDTH)
                .clamp(200.0, 380.0)
                .min(ui.available_width());
            row(
                ui,
                "Address:",
                "An Anthropic-compatible server. A local one keeps the text on this machine.",
                |ui, label| {
                    let edit = ui
                        .add(egui::TextEdit::singleline(&mut self.cleanup.url).desired_width(wide));
                    self.cleanup_dirty |= labelled(edit, label).changed();
                },
            );
            row(
                ui,
                "Send:",
                "Each request takes about a second. Smart skips it for text with no filler, correction or spoken edit in it, which is most of what you say.",
                |ui, label| {
                    self.cleanup_dirty |= combo(
                        ui,
                        "cleanup-mode",
                        label,
                        &mut self.cleanup.mode,
                        &CleanupMode::ALL,
                        mode_label,
                    );
                },
            );
            row(ui, "Model:", "Any model name the endpoint accepts.", |ui, label| {
                let edit =
                    ui.add(egui::TextEdit::singleline(&mut self.cleanup.model).desired_width(wide));
                self.cleanup_dirty |= labelled(edit, label).changed();
            });
            row(
                ui,
                "Time limit:",
                "Seconds to wait before typing the raw text instead.",
                |ui, label| {
                    let slider = ui.add(
                        egui::DragValue::new(&mut self.cleanup.budget_s)
                            .range(0.5..=30.0)
                            .speed(0.1)
                            .fixed_decimals(1)
                            .suffix(" s"),
                    );
                    self.cleanup_dirty |= labelled(slider, label)
                        .on_hover_cursor(egui::CursorIcon::Text)
                        .changed();
                },
            );
            let (tone, text) = if self.cleanup.is_local() {
                (
                    p.muted,
                    "This address is on this machine. Nothing leaves it.",
                )
            } else {
                (
                    ui.visuals().warn_fg_color,
                    "Dictated text is sent to this address, along with your key.",
                )
            };
            row(ui, "", "", |ui, _| {
                ui.label(RichText::new(text).color(tone));
            });
            if let Some(name) = overriding_variable() {
                row(
                    ui,
                    "",
                    &format!(
                        "{name} is set in this window's environment and beats this page, so the address above may not be the one in use."
                    ),
                    |_, _| {},
                );
            }
        });

        section(ui, "API key");
        let state = self.key.lock().unwrap().clone();
        let working = state == Key::Checking;
        let status = match &state {
            Key::Checking => "Checking…".to_owned(),
            Key::Stored => "A key is stored in your desktop's wallet.".to_owned(),
            Key::Missing => "No key is stored. ANTHROPIC_API_KEY in the environment is used if set."
                .to_owned(),
            Key::NoTool => "No wallet tool found (secret-tool). Set ANTHROPIC_API_KEY in the environment instead."
                .to_owned(),
            Key::Failed(why) => format!("The wallet said: {why}"),
        };
        row(ui, "Status:", "", |ui, _| {
            let color = if matches!(state, Key::Failed(_)) {
                ui.visuals().error_fg_color
            } else {
                p.muted
            };
            live(&ui.label(RichText::new(status).color(color)));
        });
        let usable = state != Key::NoTool;
        ui.add_enabled_ui(usable && !working, |ui| {
            row(
                ui,
                "New key:",
                "Kept in the wallet, never in a settings file. It cannot be shown again once saved.",
                |ui, label| {
                    let edit = ui.add(
                        egui::TextEdit::singleline(&mut self.key_input)
                            .password(true)
                            .hint_text("sk-ant-…")
                            .desired_width(ui.available_width().min(300.0)),
                    );
                    labelled(edit, label);
                    ui.horizontal(|ui| {
                        let has_input = !self.key_input.trim().is_empty();
                        if ui
                            .add_enabled(has_input, egui::Button::new("Save key"))
                            .clicked()
                        {
                            let mut entered = std::mem::take(&mut self.key_input);
                            let trimmed = entered.trim().to_owned();
                            entered.clear();
                            self.run_key(ui.ctx(), move || secret::store(&trimmed));
                        }
                        if ui
                            .add_enabled(state == Key::Stored, egui::Button::new("Remove key"))
                            .clicked()
                        {
                            self.run_key(ui.ctx(), secret::clear);
                        }
                    });
                },
            );
        });
    }

    fn speech(&mut self, ui: &mut egui::Ui) -> bool {
        let mut changed = false;
        if let Some(notice) = &self.notice {
            ui.add_space(10.0);
            live(&ui.label(RichText::new(notice).color(ui.visuals().error_fg_color)));
        }
        section(ui, "Recognition");
        let multilingual = self.multilingual;
        row(
            ui,
            "Language:",
            if multilingual {
                "Detect automatically picks per sentence. Switching reloads the model, which takes a moment."
            } else {
                "Only English is installed. Run vinowhisper-setup --language auto to add the others."
            },
            |ui, label| {
                ui.add_enabled_ui(multilingual, |ui| {
                    if combo(
                        ui,
                        "language",
                        label,
                        &mut self.speech.language,
                        &Language::ALL,
                        language_label,
                    ) {
                        self.speech = self.speech.normalised();
                        changed = true;
                    }
                });
            },
        );
        let foreign = multilingual && self.speech.language != Language::En;
        row(
            ui,
            "Translation:",
            "Captions arrive in English whatever was spoken.",
            |ui, _| {
                ui.add_enabled_ui(foreign, |ui| {
                    let mut translate = self.speech.task == Task::Translate;
                    if ui
                        .checkbox(&mut translate, "Translate to English")
                        .changed()
                    {
                        self.speech.task = if translate {
                            Task::Translate
                        } else {
                            Task::Transcribe
                        };
                        changed = true;
                    }
                });
            },
        );
        changed
    }
}

fn overriding_variable() -> Option<&'static str> {
    [
        "VINOWHISPER_CLEANUP",
        "VINOWHISPER_CLEANUP_URL",
        "VINOWHISPER_CLEANUP_MODEL",
        "VINOWHISPER_CLEANUP_BUDGET_S",
    ]
    .into_iter()
    .find(|name| std::env::var_os(name).is_some())
}

fn keys(ui: &mut egui::Ui, trigger: &str) {
    let p = theme::palette(ui.ctx());
    if trigger.is_empty() {
        ui.label(RichText::new("Not bound").color(p.muted));
        return;
    }
    egui::Frame::NONE
        .fill(p.code_bg)
        .stroke(Stroke::new(1.0_f32, p.rule))
        .corner_radius(4.0)
        .inner_margin(egui::Margin::symmetric(8, 3))
        .show(ui, |ui| {
            ui.label(RichText::new(trigger).family(theme::bold()));
        });
}

fn command(ui: &mut egui::Ui, text: &str) {
    ui.label(RichText::new(text).monospace());
}

fn heading(response: &egui::Response, title: &str, level: usize) {
    response.ctx.accesskit_node_builder(response.id, |node| {
        node.set_role(Role::Heading);
        node.set_label(title);
        node.set_level(level);
    });
}

// A status line a screen reader should read out when it changes, without moving focus.
fn live(response: &egui::Response) {
    response
        .ctx
        .accesskit_node_builder(response.id, |node| node.set_live(Live::Polite));
}

fn section(ui: &mut egui::Ui, title: &str) {
    ui.add_space(14.0);
    heading(
        &ui.label(RichText::new(title).family(theme::bold()).size(14.0)),
        title,
        2,
    );
    ui.add_space(6.0);
}

// Like a Kirigami FormLayout: right-aligned labels beside their controls, or above them
// when the window is too narrow for two columns.
// The control is handed the label's id, for screen readers to name it by.
fn row(
    ui: &mut egui::Ui,
    label: &str,
    help: &str,
    control: impl FnOnce(&mut egui::Ui, Option<egui::Id>),
) {
    let p = theme::palette(ui.ctx());
    let help = |ui: &mut egui::Ui| {
        if !help.is_empty() {
            ui.add(egui::Label::new(RichText::new(help).size(12.5).color(p.muted)).wrap());
        }
    };
    let width = ui.available_width();
    if width >= TWO_COLUMNS_FROM {
        let column = width - LABEL_WIDTH - ui.spacing().item_spacing.x;
        ui.horizontal_top(|ui| {
            let name = ui
                .allocate_ui_with_layout(
                    egui::vec2(LABEL_WIDTH, ui.spacing().interact_size.y),
                    Layout::right_to_left(Align::Center),
                    |ui| {
                        ui.set_min_width(LABEL_WIDTH);
                        (!label.is_empty()).then(|| ui.label(label).id)
                    },
                )
                .inner;
            ui.vertical(|ui| {
                // A horizontal parent leaves its children unbounded; wrap at the window's edge.
                ui.set_max_width(column);
                control(ui, name);
                help(ui);
            });
        });
    } else {
        let name = (!label.is_empty()).then(|| ui.label(label).id);
        control(ui, name);
        help(ui);
    }
    ui.add_space(8.0);
}

fn tab_name(page: Page) -> String {
    if page.beta() {
        format!("{} (beta)", page.title())
    } else {
        page.title().to_owned()
    }
}

// Painted by hand, so they say what they are themselves: a tab, its name, whether it is the
// one shown. Enter and Space click a focused response, and the ring shows which has focus.
fn page_tab(ui: &egui::Ui, response: &egui::Response, page: Page, selected: bool) {
    let name = tab_name(page);
    response.widget_info(|| {
        egui::WidgetInfo::selected(egui::WidgetType::SelectableLabel, true, selected, &name)
    });
    ui.ctx().accesskit_node_builder(response.id, |node| {
        node.set_role(Role::Tab);
        node.clear_toggled();
        node.set_selected(selected);
    });
    if response.has_focus() {
        ui.painter().rect_stroke(
            response.rect,
            4.0,
            ui.visuals().selection.stroke,
            egui::StrokeKind::Inside,
        );
    }
}

fn sidebar_item(ui: &mut egui::Ui, page: Page, current: &mut Page) {
    let p = theme::palette(ui.ctx());
    let selected = *current == page;
    let (rect, response) =
        ui.allocate_exact_size(egui::vec2(ui.available_width(), 32.0), Sense::click());
    let fill = if selected {
        theme::selected_fill(p)
    } else if response.hovered() {
        p.code_bg
    } else {
        Color32::TRANSPARENT
    };
    ui.painter().rect_filled(rect, 4.0, fill);
    // Bold and a bar as well as the tint, so the current page does not rest on colour alone.
    if selected {
        ui.painter().rect_filled(
            egui::Rect::from_min_size(rect.min, egui::vec2(3.0, rect.height())),
            egui::CornerRadius {
                nw: 4,
                sw: 4,
                ..Default::default()
            },
            p.link,
        );
    }
    ui.painter().text(
        rect.left_center() + egui::vec2(10.0, 0.0),
        egui::Align2::LEFT_CENTER,
        page.title(),
        if selected {
            FontId::new(14.0, theme::bold())
        } else {
            FontId::proportional(14.0)
        },
        p.text,
    );
    if page.beta() {
        ui.painter().text(
            rect.right_center() - egui::vec2(10.0, 0.0),
            egui::Align2::RIGHT_CENTER,
            "beta",
            FontId::proportional(11.5),
            p.muted,
        );
    }
    page_tab(ui, &response, page, selected);
    if response.clicked() {
        *current = page;
    }
}

fn tab_item(ui: &mut egui::Ui, page: Page, current: &mut Page, header: theme::Header) {
    let active = *current == page;
    let galley = ui.painter().layout_no_wrap(
        tab_name(page),
        if active {
            FontId::new(14.0, theme::bold())
        } else {
            FontId::proportional(14.0)
        },
        // left for the paint call below, so hovering can change it
        Color32::PLACEHOLDER,
    );
    let (rect, response) =
        ui.allocate_exact_size(galley.size() + egui::vec2(0.0, 6.0), Sense::click());
    let lit = active || response.hovered();
    ui.painter().galley(
        rect.left_top(),
        galley,
        if lit { header.text } else { header.muted },
    );
    if active {
        let p = theme::palette(ui.ctx());
        ui.painter().hline(
            rect.left()..=rect.right(),
            rect.bottom(),
            Stroke::new(2.0_f32, p.link),
        );
    }
    page_tab(ui, &response, page, active);
    if response.clicked() {
        *current = page;
    }
}

impl eframe::App for SettingsWindow {
    fn on_exit(&mut self, _gl: Option<&eframe::glow::Context>) {
        if self.cleanup_dirty {
            self.cleanup.save();
        }
    }

    fn update(&mut self, ctx: &egui::Context, _frame: &mut eframe::Frame) {
        let focused = ctx.input(|input| input.focused);
        if focused && !self.focused {
            // The desktop's light/dark switch may have flipped while we were in the background.
            theme::follow_desktop(ctx);
            self.kde_header = theme::kde_header();
            self.follow_text_scale(ctx, theme::desktop_text_scale());
        }
        let regained = focused && !self.focused;
        self.focused = focused;
        if self.page == Page::Shortcuts && (regained || self.shown != Some(Page::Shortcuts)) {
            self.ask_shortcuts(ctx, Duration::ZERO);
        }
        if self.shown != Some(Page::Cleanup) && self.page == Page::Cleanup {
            self.run_key(ctx, secret::status);
        }
        self.shown = Some(self.page);
        if let Some(reason) = self.refused.lock().unwrap().take() {
            // The tray put the file back; show what is really in use.
            self.speech = Speech::load();
            self.notice = Some(reason);
        }
        let (saved_settings, saved_speech) = self.show(ctx);

        if self.cleanup_dirty {
            self.cleanup_dirty = false;
            self.cleanup.save();
        }
        if saved_settings {
            self.settings.save();
            forward::reload_tray(|_| {});
        }
        if saved_speech {
            self.speech.save();
            self.notice = None;
            let refused = Arc::clone(&self.refused);
            let ctx = ctx.clone();
            forward::reload_tray(move |reason| {
                *refused.lock().unwrap() = Some(reason);
                ctx.request_repaint();
            });
        }
    }
}

impl SettingsWindow {
    // Draws the window and says which files changed; saving them, and every other side
    // effect but the buttons', is left to `update`, so tests can drive this alone.
    fn show(&mut self, ctx: &egui::Context) -> (bool, bool) {
        ctx.accesskit_node_builder(egui::accesskit_root_id(), |node| node.set_label(TITLE));
        let p = theme::palette(ctx);
        let header = theme::header(ctx, self.kde_header.as_ref(), self.focused);
        let wide = ctx.content_rect().width() >= SIDEBAR_FROM;
        let mut saved_settings = false;
        let mut saved_speech = false;

        egui::TopBottomPanel::top("header")
            .frame(
                egui::Frame::NONE
                    .fill(header.bg)
                    .inner_margin(egui::Margin::symmetric(16, 10)),
            )
            .show(ctx, |ui| {
                // Wrapped: at the desktop's largest text the four tabs don't fit on one line.
                ui.horizontal_wrapped(|ui| {
                    if wide {
                        ui.add_space(SIDEBAR_WIDTH - 16.0 + 24.0);
                        let title = ui.label(
                            RichText::new(self.page.title())
                                .family(theme::bold())
                                .size(16.0)
                                .color(header.text),
                        );
                        heading(&title, self.page.title(), 1);
                    } else {
                        ui.spacing_mut().item_spacing.x = 18.0;
                        for page in Page::ALL {
                            tab_item(ui, page, &mut self.page, header);
                        }
                    }
                });
            });

        if wide {
            egui::SidePanel::left("pages")
                .resizable(false)
                .exact_width(SIDEBAR_WIDTH)
                .frame(
                    egui::Frame::NONE
                        .fill(p.bg)
                        .inner_margin(egui::Margin::same(8)),
                )
                .show(ctx, |ui| {
                    ui.spacing_mut().item_spacing.y = 2.0;
                    for page in Page::ALL {
                        sidebar_item(ui, page, &mut self.page);
                    }
                });
        }

        egui::CentralPanel::default()
            .frame(
                egui::Frame::NONE
                    .fill(p.bg)
                    // No right margin: the scroll bar belongs on the window's edge, and the
                    // content keeps its own gap from it below.
                    .inner_margin(egui::Margin {
                        left: 24,
                        right: 0,
                        top: 4,
                        bottom: 4,
                    }),
            )
            .show(ctx, |ui| {
                // A solid bar, always drawn: the floating one only shows on hover, so a page
                // that runs past the window gave no sign there was more below.
                ui.spacing_mut().scroll = egui::style::ScrollStyle::solid();
                let scrolled = egui::ScrollArea::vertical()
                    .auto_shrink([false, false])
                    .scroll_bar_visibility(egui::scroll_area::ScrollBarVisibility::AlwaysVisible)
                    // Drag-to-scroll is for touch, and its invisible page-sized area took a Tab stop.
                    .scroll_source(egui::scroll_area::ScrollSource {
                        drag: false,
                        ..Default::default()
                    })
                    .show(ui, |ui| {
                        ui.set_max_width((ui.available_width() - SCROLL_GAP).min(FORM_MAX_WIDTH));
                        match self.page {
                            Page::Shortcuts => self.shortcuts(ui),
                            Page::Captions => saved_settings = self.captions(ui),
                            Page::Speech => saved_speech = self.speech(ui),
                            Page::Cleanup => self.cleanup(ui),
                        }
                        ui.add_space(12.0);
                    });
                // egui's bar takes focus on Tab with no name; its id is the area's with axis 1.
                ctx.accesskit_node_builder(scrolled.id.with(1usize), |node| {
                    node.set_role(Role::ScrollBar);
                    node.set_label(format!("Scroll {}", self.page.title()));
                    node.set_orientation(egui::accesskit::Orientation::Vertical);
                });
            });
        (saved_settings, saved_speech)
    }
}

const TITLE: &str = "vinoWhisper settings";

pub fn run(app_id: &str, page: Option<&str>) -> ExitCode {
    let page = page.and_then(Page::parse);
    let options = eframe::NativeOptions {
        viewport: egui::ViewportBuilder::default()
            .with_title(TITLE)
            .with_app_id(app_id)
            .with_inner_size([820.0, 740.0])
            .with_min_inner_size([340.0, 320.0]),
        // With vsync, a swap waits on a frame callback KWin holds back from a window it
        // considers hidden, and the window stops answering pings. It only redraws on input.
        vsync: false,
        ..Default::default()
    };
    let result = eframe::run_native(
        TITLE,
        options,
        Box::new(|cc| {
            theme::install(&cc.egui_ctx);
            let mut window = SettingsWindow::new(page);
            // Before the first frame, not at first focus: a window opened in the background
            // would otherwise draw its first frames small.
            window.follow_text_scale(&cc.egui_ctx, theme::desktop_text_scale());
            Ok(Box::new(window))
        }),
    );
    match result {
        Ok(()) => ExitCode::SUCCESS,
        Err(err) => {
            eprintln!("[vinowhisper-gui] {err}");
            ExitCode::FAILURE
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use egui_kittest::Harness;
    use egui_kittest::kittest::{NodeT, Queryable};

    // No file is read or written and nothing is spawned: `show` is the window minus its
    // side effects, and the tests never press the buttons that run the tray or the wallet.
    struct Under {
        window: SettingsWindow,
        styled: bool,
        saved: (bool, bool),
    }

    fn harness(page: Page, width: f32) -> Harness<'static, Under> {
        let mut window = SettingsWindow::with(
            Some(page),
            Settings::default(),
            Speech::default(),
            Cleanup::default(),
        );
        window.multilingual = true;
        *window.key.lock().unwrap() = Key::Stored;
        *window.shortcuts.lock().unwrap() = Shortcuts::Bound {
            captions: "Meta+Alt+C".into(),
            dictate: "Meta+H".into(),
            configurable: true,
        };
        Harness::builder().with_size((width, 740.0)).build_state(
            |ctx, under: &mut Under| {
                // Fonts only bind at the next pass, and the bold family is not stock.
                if !under.styled {
                    theme::apply_for_tests(ctx);
                    under.styled = true;
                    ctx.request_repaint();
                    return;
                }
                let (settings, speech) = under.window.show(ctx);
                under.saved.0 |= settings;
                under.saved.1 |= speech;
            },
            Under {
                window,
                styled: false,
                saved: (false, false),
            },
        )
    }

    const WIDE: f32 = 820.0;
    const NARROW: f32 = 400.0;

    #[test]
    fn every_page_is_a_named_tab_and_only_the_current_one_is_selected() {
        for width in [WIDE, NARROW] {
            let mut harness = harness(Page::Captions, width);
            harness.run();
            for page in Page::ALL {
                let name = tab_name(page);
                let tab = harness.get_by_role_and_label(Role::Tab, &name);
                assert_eq!(
                    tab.accesskit_node().is_selected(),
                    Some(page == Page::Captions),
                    "{} at {width}",
                    page.title()
                );
            }
        }
    }

    #[test]
    fn a_tab_opens_its_page_by_pointer_and_by_keyboard() {
        let mut harness = harness(Page::Shortcuts, WIDE);
        harness.run();
        harness.get_by_role_and_label(Role::Tab, "Speech").click();
        harness.run();
        assert!(harness.state().window.page == Page::Speech);

        harness.get_by_role_and_label(Role::Tab, "Captions").focus();
        harness.run();
        assert!(
            harness
                .get_by_role_and_label(Role::Tab, "Captions")
                .is_focused()
        );
        harness.key_press(egui::Key::Enter);
        harness.run();
        assert!(harness.state().window.page == Page::Captions);
    }

    #[test]
    fn tab_moves_focus_from_one_page_tab_to_the_next() {
        let mut harness = harness(Page::Shortcuts, WIDE);
        harness.run();
        harness
            .get_by_role_and_label(Role::Tab, "Shortcuts")
            .focus();
        harness.run();
        harness.key_press(egui::Key::Tab);
        harness.run();
        assert!(
            harness
                .get_by_role_and_label(Role::Tab, "Captions")
                .is_focused()
        );
        harness.key_press(egui::Key::Space);
        harness.run();
        assert!(harness.state().window.page == Page::Captions);
    }

    #[test]
    fn the_captions_page_names_every_control_after_its_label() {
        for width in [WIDE, NARROW] {
            let mut harness = harness(Page::Captions, width);
            harness.run();
            for label in ["Position:", "Text size:", "Listen to:"] {
                harness.get_by_role_and_label(Role::ComboBox, label);
            }
            for label in ["Hide after silence:", "Unload after:"] {
                harness.get_by_role_and_label(Role::SpinButton, label);
            }
            harness.get_by_role_and_label(Role::CheckBox, "Save transcripts");
            for title in ["Overlay", "Listening", "Tray"] {
                harness.get_by_role_and_label(Role::Heading, title);
            }
        }
    }

    #[test]
    fn a_combo_box_can_be_changed_and_says_what_it_is_set_to() {
        let mut harness = harness(Page::Captions, WIDE);
        harness.run();
        let position = harness.get_by_role_and_label(Role::ComboBox, "Position:");
        assert_eq!(position.value().as_deref(), Some("Bottom of the screen"));
        position.click();
        harness.run();
        harness.get_by_label("Top of the screen").click();
        harness.run();
        assert_eq!(harness.state().window.settings.position, Position::Top);
        assert!(harness.state().saved.0);
        assert_eq!(
            harness
                .get_by_role_and_label(Role::ComboBox, "Position:")
                .value()
                .as_deref(),
            Some("Top of the screen")
        );
    }

    #[test]
    fn escape_closes_an_open_combo_box_without_changing_it() {
        let mut harness = harness(Page::Captions, WIDE);
        harness.run();
        harness
            .get_by_role_and_label(Role::ComboBox, "Text size:")
            .click();
        harness.run();
        assert!(harness.query_by_label("Large").is_some());
        harness.key_press(egui::Key::Escape);
        harness.run();
        assert!(harness.query_by_label("Large").is_none());
        assert!(!harness.state().saved.0);
    }

    #[test]
    fn a_checkbox_reports_and_flips_its_state() {
        let mut harness = harness(Page::Captions, WIDE);
        harness.run();
        let check = harness.get_by_role_and_label(Role::CheckBox, "Save transcripts");
        let before = check.accesskit_node().toggled();
        check.focus();
        harness.run();
        harness.key_press(egui::Key::Space);
        harness.run();
        assert!(harness.state().window.settings.save_transcripts);
        assert!(harness.state().saved.0);
        let after = harness
            .get_by_role_and_label(Role::CheckBox, "Save transcripts")
            .accesskit_node()
            .toggled();
        assert_ne!(before, after);
    }

    #[test]
    fn the_speech_page_names_its_controls() {
        let mut harness = harness(Page::Speech, WIDE);
        harness.run();
        harness.get_by_role_and_label(Role::ComboBox, "Language:");
        harness.get_by_role_and_label(Role::CheckBox, "Translate to English");
    }

    #[test]
    fn the_cleanup_page_names_its_fields_and_the_key_is_a_password_field() {
        let mut harness = harness(Page::Cleanup, WIDE);
        harness.state_mut().window.cleanup.enabled = true;
        harness.run();
        harness.get_by_role_and_label(Role::CheckBox, "Clean up dictation");
        for label in ["Address:", "Model:"] {
            harness.get_by_role_and_label(Role::TextInput, label);
        }
        harness.get_by_role_and_label(Role::ComboBox, "Send:");
        harness.get_by_role_and_label(Role::SpinButton, "Time limit:");
        let save = harness.get_by_role_and_label(Role::Button, "Save key");
        assert!(save.accesskit_node().is_disabled(), "nothing typed yet");
        let key = harness.get_by_role_and_label(Role::PasswordInput, "New key:");
        assert_eq!(key.accesskit_node().placeholder(), Some("sk-ant-…"));
        key.focus();
        harness.run();
        harness
            .get_by_role_and_label(Role::PasswordInput, "New key:")
            .type_text("sk-secret");
        harness.run();
        assert_eq!(harness.state().window.key_input, "sk-secret");
        let shown = harness
            .get_by_role_and_label(Role::PasswordInput, "New key:")
            .value()
            .unwrap_or_default();
        assert!(!shown.contains("secret"), "{shown}");
        harness.get_by_role_and_label(Role::Button, "Save key");
        harness.get_by_role_and_label(Role::Button, "Remove key");
    }

    #[test]
    fn the_key_status_is_announced_when_it_changes() {
        let mut harness = harness(Page::Cleanup, WIDE);
        harness.run();
        let status = harness.get_by_label("A key is stored in your desktop's wallet.");
        assert_eq!(status.accesskit_node().live(), Live::Polite);
    }

    #[test]
    fn fields_left_unused_by_cleanup_being_off_say_so() {
        let mut harness = harness(Page::Cleanup, WIDE);
        harness.run();
        assert!(!harness.state().window.cleanup.enabled);
        let address = harness.get_by_role_and_label(Role::TextInput, "Address:");
        assert!(address.accesskit_node().is_disabled());
    }

    #[test]
    fn the_shortcuts_page_reads_the_keys_and_offers_change() {
        let mut harness = harness(Page::Shortcuts, WIDE);
        harness.run();
        harness.get_by_label("Meta+Alt+C");
        harness.get_by_label("Meta+H");
        harness.get_by_role_and_label(Role::Button, "Change…");
        harness.get_by_role_and_label(Role::Heading, "Shortcuts");
    }

    #[test]
    fn nothing_focusable_is_nameless() {
        for page in Page::ALL {
            for width in [WIDE, NARROW] {
                let mut harness = harness(page, width);
                harness.state_mut().window.cleanup.enabled = true;
                harness.run();
                let nameless: Vec<String> = harness
                    .query_all_by(|node| {
                        node.data().supports_action(egui::accesskit::Action::Focus)
                            && node.label().is_none_or(|label| label.trim().is_empty())
                    })
                    .map(|node| format!("{:?} at {:?}", node.accesskit_node().role(), node.rect()))
                    .collect();
                assert!(
                    nameless.is_empty(),
                    "{} at {width}: {nameless:?}",
                    page.title()
                );
            }
        }
    }

    #[test]
    fn the_window_is_named() {
        let mut harness = harness(Page::Captions, WIDE);
        harness.run();
        assert_eq!(
            harness.root().accesskit_node().label().as_deref(),
            Some(TITLE)
        );
    }

    #[test]
    fn large_text_keeps_every_control_inside_the_window() {
        // 1.5 is the step where 820 wide first folds the sidebar; 3.0 is the clamp.
        for zoom in [1.5, *theme::TEXT_SCALE.end()] {
            for page in Page::ALL {
                let mut harness = harness(page, WIDE);
                harness.state_mut().window.cleanup.enabled = true;
                harness.ctx.set_zoom_factor(zoom);
                harness.run();
                let outside: Vec<String> = harness
                    .query_all_by(|node| {
                        node.data().supports_action(egui::accesskit::Action::Focus)
                    })
                    .filter(|node| node.rect().max.x > WIDE + 0.5 || node.rect().min.x < -0.5)
                    .map(|node| format!("{:?} {:?}", node.accesskit_node().label(), node.rect()))
                    .collect();
                assert!(
                    outside.is_empty(),
                    "{} at {zoom}x: {outside:?}",
                    page.title()
                );
            }
        }
    }

    #[test]
    fn the_desktop_text_scale_sets_zoom_but_keeps_a_later_ctrl_plus() {
        let mut harness = harness(Page::Captions, WIDE);
        harness.run();
        let ctx = harness.ctx.clone();
        // Zoom lands at the start of the next pass.
        let follow = |harness: &mut Harness<'static, Under>, scale| {
            harness.state_mut().window.follow_text_scale(&ctx, scale);
            harness.run();
            ctx.zoom_factor()
        };
        assert_eq!(follow(&mut harness, Some(1.25)), 1.25);
        harness.ctx.set_zoom_factor(1.5);
        harness.run();
        assert_eq!(
            follow(&mut harness, Some(1.25)),
            1.5,
            "unchanged desktop setting, user zoom kept"
        );
        assert_eq!(
            follow(&mut harness, None),
            1.0,
            "the desktop stopped asking"
        );
    }

    #[test]
    fn doubled_text_size_folds_the_sidebar_and_keeps_every_control() {
        let mut harness = harness(Page::Captions, WIDE);
        harness.ctx.set_zoom_factor(2.0);
        harness.run();
        assert!(harness.ctx.content_rect().width() < SIDEBAR_FROM);
        harness.get_by_role_and_label(Role::Tab, "Cleanup (beta)");
        harness.get_by_role_and_label(Role::ComboBox, "Position:");
        harness.get_by_role_and_label(Role::CheckBox, "Save transcripts");
    }
}
