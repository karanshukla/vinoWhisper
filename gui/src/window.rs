use std::process::ExitCode;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use eframe::egui::{self, Align, Color32, FontId, Layout, RichText, Sense, Stroke};

use crate::forward::{self, Shortcuts};
use crate::secret::{self, Key};
use crate::theme;
use vinowhisper_settings::{
    Cleanup, Language, Position, Settings, Source, Speech, Task, TextSize, multilingual_installed,
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

fn combo<T: Copy + PartialEq>(
    ui: &mut egui::Ui,
    id: &str,
    value: &mut T,
    all: &[T],
    label: fn(T) -> &'static str,
) -> bool {
    let before = *value;
    egui::ComboBox::from_id_salt(id)
        .width(COMBO_WIDTH)
        .selected_text(label(*value))
        .show_ui(ui, |ui| {
            for &option in all {
                ui.selectable_value(value, option, label(option));
            }
        });
    *value != before
}

fn number<N: egui::emath::Numeric>(
    ui: &mut egui::Ui,
    value: &mut N,
    range: std::ops::RangeInclusive<N>,
    suffix: &str,
) -> bool {
    ui.add(egui::DragValue::new(value).range(range).suffix(suffix))
        // DragValue's own hover cursor is a horizontal resize arrow, which reads as a splitter here.
        .on_hover_cursor(egui::CursorIcon::Text)
        .changed()
}

// Below this the sidebar folds into tabs in the header, and form labels go above their control.
const SIDEBAR_FROM: f32 = 620.0;
const SIDEBAR_WIDTH: f32 = 180.0;
const LABEL_WIDTH: f32 = 150.0;
const FORM_MAX_WIDTH: f32 = 620.0;
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
    shortcuts: Arc<Mutex<Shortcuts>>,
    // Which page last asked the tray, so arriving on Shortcuts asks again.
    shown: Option<Page>,
    refused: Arc<Mutex<Option<String>>>,
    notice: Option<String>,
}

impl SettingsWindow {
    fn new(page: Option<Page>) -> Self {
        SettingsWindow {
            page: page.unwrap_or(Page::Shortcuts),
            settings: Settings::load(),
            speech: Speech::load(),
            cleanup: Cleanup::load(),
            cleanup_dirty: false,
            key: Arc::new(Mutex::new(Key::Checking)),
            key_input: String::new(),
            multilingual: multilingual_installed(),
            focused: true,
            kde_header: theme::kde_header(),
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
                row(ui, "", "", |ui| {
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
                    |ui| keys(ui, &captions),
                );
                row(
                    ui,
                    "Dictation:",
                    "Hold to talk and release to type, or tap to start and tap again to finish.",
                    |ui| keys(ui, &dictate),
                );
                if configurable {
                    row(ui, "", "Opens your desktop's shortcut settings.", |ui| {
                        if ui.button("Change…").clicked() {
                            forward::run_tray(&["configure-shortcut"]);
                        }
                    });
                } else {
                    row(
                        ui,
                        "",
                        "Change them in your desktop's shortcut settings, where they are listed under vinoWhisper.",
                        |_| {},
                    );
                }
            }
            Shortcuts::Stopped => {
                row(
                    ui,
                    "",
                    "vinoWhisper is not running, so no shortcut is active.",
                    |ui| {
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
                row(ui, "Captions:", "", |ui| {
                    command(ui, "vinowhisper-tray toggle");
                });
                row(
                    ui,
                    "Dictation:",
                    "On the key's press and release, for hold to talk. Or vinowhisper-tray dictate on one key, to tap.",
                    |ui| {
                        command(ui, "vinowhisper-tray dictate-press");
                        command(ui, "vinowhisper-tray dictate-release");
                    },
                );
            }
            Shortcuts::Unknown(reason) => {
                row(ui, "", "", |ui| {
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
            |ui| {
                changed |= combo(
                    ui,
                    "position",
                    &mut self.settings.position,
                    &Position::ALL,
                    position_label,
                );
            },
        );
        row(ui, "Text size:", "", |ui| {
            changed |= combo(
                ui,
                "size",
                &mut self.settings.size,
                &TextSize::ALL,
                size_label,
            );
        });
        section(ui, "Listening");
        row(ui, "Listen to:", "What the captions follow.", |ui| {
            changed |= combo(
                ui,
                "source",
                &mut self.settings.source,
                &Source::ALL,
                source_label,
            );
        });
        row(
            ui,
            "Hide after silence:",
            "Seconds with no audio before the captions count down and hide. 0 keeps them up.",
            |ui| {
                changed |= number(ui, &mut self.settings.hide_after_silence_s, 0..=3600, " s");
            },
        );
        row(
            ui,
            "Transcripts:",
            "Write each session to a file you can read back later.",
            |ui| {
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
            |ui| {
                changed |= number(ui, &mut self.settings.tray_idle_minutes, 0..=1440, " min");
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
        section(ui, "Dictation cleanup");
        row(
            ui,
            "Clean up:",
            "Sends what you dictate to a language model before it is typed, to drop ums, apply \
             corrections and act on \"scratch that\". It only ever deletes words, and any other \
             answer is thrown away and the raw text is typed. Off by default.",
            |ui| {
                self.cleanup_dirty |= ui
                    .checkbox(&mut self.cleanup.enabled, "Clean up dictation")
                    .changed();
            },
        );
        let off = !self.cleanup.enabled;
        ui.add_enabled_ui(!off, |ui| {
            section(ui, "Endpoint");
            let wide = (ui.available_width() - LABEL_WIDTH).clamp(200.0, 380.0);
            row(
                ui,
                "Address:",
                "An Anthropic-compatible server. A local one keeps the text on this machine.",
                |ui| {
                    let edit = ui
                        .add(egui::TextEdit::singleline(&mut self.cleanup.url).desired_width(wide));
                    self.cleanup_dirty |= edit.changed();
                },
            );
            row(ui, "Model:", "Any model name the endpoint accepts.", |ui| {
                let edit =
                    ui.add(egui::TextEdit::singleline(&mut self.cleanup.model).desired_width(wide));
                self.cleanup_dirty |= edit.changed();
            });
            row(
                ui,
                "Time limit:",
                "Seconds to wait before typing the raw text instead.",
                |ui| {
                    let slider = ui.add(
                        egui::DragValue::new(&mut self.cleanup.budget_s)
                            .range(0.5..=30.0)
                            .speed(0.1)
                            .fixed_decimals(1)
                            .suffix(" s"),
                    );
                    self.cleanup_dirty |= slider.on_hover_cursor(egui::CursorIcon::Text).changed();
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
            row(ui, "", "", |ui| {
                ui.label(RichText::new(text).color(tone));
            });
            if let Some(name) = overriding_variable() {
                row(
                    ui,
                    "",
                    &format!(
                        "{name} is set in this window's environment and beats this page, so the address above may not be the one in use."
                    ),
                    |_| {},
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
        row(ui, "Status:", "", |ui| {
            let color = if matches!(state, Key::Failed(_)) {
                ui.visuals().error_fg_color
            } else {
                p.muted
            };
            ui.label(RichText::new(status).color(color));
        });
        let usable = state != Key::NoTool;
        ui.add_enabled_ui(usable && !working, |ui| {
            row(
                ui,
                "New key:",
                "Kept in the wallet, never in a settings file. It cannot be shown again once saved.",
                |ui| {
                    ui.add(
                        egui::TextEdit::singleline(&mut self.key_input)
                            .password(true)
                            .hint_text("sk-ant-…")
                            .desired_width(300.0),
                    );
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
            ui.label(RichText::new(notice).color(ui.visuals().error_fg_color));
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
            |ui| {
                ui.add_enabled_ui(multilingual, |ui| {
                    if combo(
                        ui,
                        "language",
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
            |ui| {
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

fn section(ui: &mut egui::Ui, title: &str) {
    ui.add_space(14.0);
    ui.label(RichText::new(title).family(theme::bold()).size(14.0));
    ui.add_space(6.0);
}

// Like a Kirigami FormLayout: right-aligned labels beside their controls, or above them
// when the window is too narrow for two columns.
fn row(ui: &mut egui::Ui, label: &str, help: &str, control: impl FnOnce(&mut egui::Ui)) {
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
            ui.allocate_ui_with_layout(
                egui::vec2(LABEL_WIDTH, ui.spacing().interact_size.y),
                Layout::right_to_left(Align::Center),
                |ui| {
                    ui.set_min_width(LABEL_WIDTH);
                    ui.label(label);
                },
            );
            ui.vertical(|ui| {
                // A horizontal parent leaves its children unbounded; wrap at the window's edge.
                ui.set_max_width(column);
                control(ui);
                help(ui);
            });
        });
    } else {
        if !label.is_empty() {
            ui.label(label);
        }
        control(ui);
        help(ui);
    }
    ui.add_space(8.0);
}

fn sidebar_item(ui: &mut egui::Ui, page: Page, current: &mut Page) {
    let p = theme::palette(ui.ctx());
    let selected = *current == page;
    let (rect, response) =
        ui.allocate_exact_size(egui::vec2(ui.available_width(), 32.0), Sense::click());
    let fill = if selected {
        p.link.gamma_multiply(0.22)
    } else if response.hovered() {
        p.code_bg
    } else {
        Color32::TRANSPARENT
    };
    ui.painter().rect_filled(rect, 4.0, fill);
    ui.painter().text(
        rect.left_center() + egui::vec2(10.0, 0.0),
        egui::Align2::LEFT_CENTER,
        page.title(),
        FontId::proportional(14.0),
        p.text,
    );
    if response.clicked() {
        *current = page;
    }
}

fn tab_item(ui: &mut egui::Ui, page: Page, current: &mut Page, header: theme::Header) {
    let active = *current == page;
    let galley = ui.painter().layout_no_wrap(
        page.title().to_owned(),
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
        let p = theme::palette(ctx);
        let header = theme::header(ctx, self.kde_header.as_ref(), focused);
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
                ui.horizontal(|ui| {
                    if wide {
                        ui.add_space(SIDEBAR_WIDTH - 16.0 + 24.0);
                        ui.label(
                            RichText::new(self.page.title())
                                .family(theme::bold())
                                .size(16.0)
                                .color(header.text),
                        );
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
                    .inner_margin(egui::Margin::symmetric(24, 4)),
            )
            .show(ctx, |ui| {
                egui::ScrollArea::vertical()
                    .auto_shrink([false, false])
                    .show(ui, |ui| {
                        ui.set_max_width(ui.available_width().min(FORM_MAX_WIDTH));
                        match self.page {
                            Page::Shortcuts => self.shortcuts(ui),
                            Page::Captions => saved_settings = self.captions(ui),
                            Page::Speech => saved_speech = self.speech(ui),
                            Page::Cleanup => self.cleanup(ui),
                        }
                        ui.add_space(12.0);
                    });
            });

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

pub fn run(app_id: &str, page: Option<&str>) -> ExitCode {
    let page = page.and_then(Page::parse);
    let options = eframe::NativeOptions {
        viewport: egui::ViewportBuilder::default()
            .with_title("vinoWhisper settings")
            .with_app_id(app_id)
            .with_inner_size([760.0, 480.0])
            .with_min_inner_size([340.0, 320.0]),
        // With vsync, a swap waits on a frame callback KWin holds back from a window it
        // considers hidden, and the window stops answering pings. It only redraws on input.
        vsync: false,
        ..Default::default()
    };
    let result = eframe::run_native(
        "vinoWhisper settings",
        options,
        Box::new(|cc| {
            theme::install(&cc.egui_ctx);
            Ok(Box::new(SettingsWindow::new(page)))
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
