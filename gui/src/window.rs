use std::path::PathBuf;
use std::process::ExitCode;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use eframe::egui::{self, Align, Layout, RichText};
use vino_ui::a11y;
use vino_ui::icons::Icon;
use vino_ui::theme;
use vino_ui::widgets::{self, Button, ChipKind, Row, Tone};

use crate::forward::{self, Shortcuts};
use crate::model::{self, Line, ModelState, Status, Tool};
use crate::sessions;
use vinowhisper_settings::{
    Cleanup, CleanupMode, Language, Position, Settings, Source, Speech, SpeechModel, Summaries,
    Task, TextSize, multilingual_installed,
};

#[derive(Clone, Copy, PartialEq, Eq)]
enum Page {
    Shortcuts,
    Captions,
    Speech,
    Cleanup,
    Sessions,
    About,
}

impl Page {
    const ALL: [Page; 6] = [
        Page::Shortcuts,
        Page::Captions,
        Page::Speech,
        Page::Cleanup,
        Page::Sessions,
        Page::About,
    ];

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
            Page::Sessions => "Sessions",
            Page::About => vino_ui::about::TITLE,
        }
    }

    fn icon(self) -> Icon {
        match self {
            Page::Shortcuts => Icon::Keyboard,
            Page::Captions => Icon::Captions,
            Page::Speech => Icon::Mic,
            Page::Cleanup => Icon::Cleanup,
            Page::Sessions => Icon::Sessions,
            Page::About => Icon::Info,
        }
    }
}

fn source_label(source: Source) -> &'static str {
    match source {
        Source::Output => "What is playing",
        Source::Mic => "Microphone",
    }
}

fn position_label(position: Position) -> &'static str {
    match position {
        Position::Bottom => "Bottom",
        Position::Top => "Top",
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
        Language::Bg => "Bulgarian",
        Language::Hr => "Croatian",
        Language::Cs => "Czech",
        Language::Da => "Danish",
        Language::Nl => "Dutch",
        Language::Et => "Estonian",
        Language::Fi => "Finnish",
        Language::El => "Greek",
        Language::Hu => "Hungarian",
        Language::It => "Italian",
        Language::Lv => "Latvian",
        Language::Lt => "Lithuanian",
        Language::Mt => "Maltese",
        Language::Pl => "Polish",
        Language::Pt => "Portuguese",
        Language::Ro => "Romanian",
        Language::Sk => "Slovak",
        Language::Sl => "Slovenian",
        Language::Sv => "Swedish",
        Language::Ru => "Russian",
        Language::Uk => "Ukrainian",
    }
}

fn model_label(model: SpeechModel) -> &'static str {
    match model {
        SpeechModel::Whisper => "Whisper small",
        SpeechModel::Canary => "Canary 1B v2",
    }
}

fn model_switch_note(wanted: Speech) -> String {
    let from = language_label(wanted.language);
    match (wanted.model, wanted.language) {
        (SpeechModel::Canary, Language::Auto) => {
            "Canary can detect the language only to translate it, so Language is now English. \
             Turn on Translate after choosing Detect automatically to keep translating."
                .to_owned()
        }
        (model, _) => format!(
            "{} has no {from}, so Language is now English.",
            model_label(model)
        ),
    }
}

fn model_help(model: SpeechModel, installed: bool) -> &'static str {
    match (model, installed) {
        (SpeechModel::Whisper, _) => {
            "OpenAI's Whisper small, 0.9 GB. Five languages, translates them into English. \
             The default, and the most accurate on English captions."
        }
        (SpeechModel::Canary, true) => {
            "NVIDIA's Canary 1B v2, 0.9 GB. 25 European languages, translates them into \
             English, with or without naming the language. Switching reloads the model."
        }
        (SpeechModel::Canary, false) => {
            "Canary 1B v2 is not downloaded yet: use Download below (0.97 GB on an NPU, \
             1.9 GB otherwise)."
        }
    }
}

const COMBO_WIDTH: f32 = 240.0;

fn combo<T: Copy + PartialEq>(
    ui: &mut egui::Ui,
    id: &str,
    named: &dyn Fn(egui::Response) -> egui::Response,
    value: &mut T,
    all: &[T],
    label: fn(T) -> &'static str,
) -> bool {
    let before = *value;
    let shown = widgets::combo(ui, id, label(*value).to_owned(), COMBO_WIDTH).show_ui(ui, |ui| {
        for &option in all {
            ui.selectable_value(value, option, label(option));
        }
    });
    named(shown.response);
    *value != before
}

fn segmented<T: Copy + PartialEq>(
    ui: &mut egui::Ui,
    name: &str,
    value: &mut T,
    all: &[T],
    label: fn(T) -> &'static str,
) -> bool {
    let options: Vec<(T, &str)> = all.iter().map(|&option| (option, label(option))).collect();
    widgets::segmented(ui, name, value, &options)
}

fn number<N: egui::emath::Numeric>(
    ui: &mut egui::Ui,
    named: &dyn Fn(egui::Response) -> egui::Response,
    value: &mut N,
    range: std::ops::RangeInclusive<N>,
    unit: &str,
) -> bool {
    // Right to left: the unit after the box.
    widgets::small(ui, unit);
    let response = ui.add(egui::DragValue::new(value).range(range));
    named(response)
        // DragValue's own hover cursor is a horizontal resize arrow, which reads as a splitter here.
        .on_hover_cursor(egui::CursorIcon::Text)
        .changed()
}

struct SettingsWindow {
    page: Page,
    settings: Settings,
    speech: Speech,
    cleanup: Cleanup,
    cleanup_dirty: bool,
    model: Arc<Mutex<ModelState>>,
    confirm_delete: bool,
    canary_model: Arc<Mutex<ModelState>>,
    confirm_delete_canary: bool,
    multilingual: bool,
    canary: bool,
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
    // Why a model switch changed the language, so it never happens silently.
    speech_note: Option<String>,
    summaries: Summaries,
    summaries_dirty: bool,
    // The sessions window's app menu entry: where it goes, whether it is there, and why
    // the last change failed.
    apps_data: PathBuf,
    in_app_menu: bool,
    app_menu_error: Option<String>,
    // From the session listing, read when the Sessions page is shown.
    model_ready: Arc<Mutex<Option<bool>>>,
    // About's Updates row.
    updater: vino_ui::update::Updater,
}

// The release installer leaves itself as ~/.local/bin/vinowhisper-upgrade, which installs a
// release's wheel, tray and this window; without it (a checkout) About links to the release.
fn updater() -> vino_ui::update::Updater {
    let updater = vino_ui::update::Updater::new(vino_ui::App::Whisper, env!("CARGO_PKG_VERSION"));
    match std::env::var_os("HOME")
        .map(|home| std::path::PathBuf::from(home).join(".local/bin/vinowhisper-upgrade"))
    {
        Some(upgrade) if upgrade.is_file() => {
            updater.with_command([upgrade.to_string_lossy().into_owned()])
        }
        _ => updater,
    }
}

impl SettingsWindow {
    fn new(page: Option<Page>) -> Self {
        let mut window = Self::with(page, Settings::load(), Speech::load(), Cleanup::load());
        window.summaries = Summaries::load();
        window.in_app_menu = vinowhisper_settings::sessions_entry_shown_in(&window.apps_data);
        window.multilingual = multilingual_installed();
        window.canary = SpeechModel::Canary.installed();
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
            model: Arc::new(Mutex::new(ModelState::default())),
            confirm_delete: false,
            canary_model: Arc::new(Mutex::new(ModelState::default())),
            confirm_delete_canary: false,
            multilingual: false,
            canary: false,
            focused: true,
            kde_header: None,
            text_scale: None,
            shortcuts: Arc::new(Mutex::new(Shortcuts::Asking)),
            shown: None,
            refused: Arc::new(Mutex::new(None)),
            notice: None,
            speech_note: None,
            summaries: Summaries::default(),
            apps_data: vinowhisper_settings::data_home(),
            in_app_menu: false,
            app_menu_error: None,
            summaries_dirty: false,
            model_ready: Arc::new(Mutex::new(None)),
            updater: updater(),
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
        let state = self.shortcuts.lock().unwrap().clone();
        widgets::group_label(ui, "Global shortcuts");
        widgets::group(ui, |ui| match state {
            Shortcuts::Asking | Shortcuts::Pending => {
                Row::new("Asking the desktop which keys are bound.").show(ui, |_, _| {});
            }
            Shortcuts::Bound {
                captions,
                dictate,
                configurable,
            } => {
                Row::new("Captions")
                    .help("Shows or hides the floating captions.")
                    .show(ui, |ui, _| keys(ui, &captions));
                Row::new("Dictation")
                    .help("Hold to talk and release to type, or tap to start and tap to finish.")
                    .show(ui, |ui, _| keys(ui, &dictate));
                if configurable {
                    Row::new("Change them")
                        .help("Opens your desktop's shortcut settings.")
                        .show(ui, |ui, _| {
                            if ui.add(Button::secondary("Change…")).clicked() {
                                forward::run_tray(&["configure-shortcut"]);
                            }
                        });
                } else {
                    Row::new("Change them")
                        .help(
                            "In your desktop's shortcut settings, where they are listed under \
                             vinoWhisper.",
                        )
                        .show(ui, |_, _| {});
                }
            }
            Shortcuts::Stopped => {
                Row::new("Not running")
                    .help("vinoWhisper is not running, so no shortcut is active.")
                    .tone(Tone::Warn)
                    .show(ui, |ui, _| {
                        if ui.add(Button::primary("Start vinoWhisper")).clicked() {
                            forward::run_tray(&["--hidden"]);
                            // The portal takes a moment to answer a fresh tray.
                            self.ask_shortcuts(ui.ctx(), Duration::from_millis(1500));
                        }
                    });
            }
            Shortcuts::Unavailable => {
                Row::new("No shortcuts portal")
                    .help(
                        "This desktop has none. Bind these commands to keys in its keyboard \
                         settings instead.",
                    )
                    .show(ui, |_, _| {});
                Row::new("Captions").show(ui, |ui, _| {
                    widgets::mono(ui, "vinowhisper-tray toggle");
                });
                Row::new("Dictation")
                    .help(
                        "On the key's press and release, for hold to talk. Or vinowhisper-tray \
                         dictate on one key, to tap.",
                    )
                    .show(ui, |ui, _| {
                        ui.with_layout(Layout::top_down(Align::Max), |ui| {
                            widgets::mono(ui, "vinowhisper-tray dictate-press");
                            widgets::mono(ui, "vinowhisper-tray dictate-release");
                        });
                    });
            }
            Shortcuts::Unknown(reason) => {
                Row::new("Unknown").help(&reason).show(ui, |_, _| {});
            }
        });
    }

    fn captions(&mut self, ui: &mut egui::Ui) -> bool {
        let mut changed = false;
        widgets::group_label(ui, "Overlay");
        widgets::group(ui, |ui| {
            widgets::group_section(ui, |ui| {
                caption_preview(ui, self.settings.position, self.settings.size);
                widgets::small(ui, "Preview");
            });
            Row::new("Position").show(ui, |ui, _| {
                changed |= segmented(
                    ui,
                    "Position",
                    &mut self.settings.position,
                    &Position::ALL,
                    position_label,
                );
            });
            Row::new("Text size").show(ui, |ui, _| {
                changed |= segmented(
                    ui,
                    "Text size",
                    &mut self.settings.size,
                    &TextSize::ALL,
                    size_label,
                );
            });
        });
        widgets::group_label(ui, "Listening");
        widgets::group(ui, |ui| {
            Row::new("Listen to")
                .help("What the captions follow.")
                .show(ui, |ui, _| {
                    changed |= segmented(
                        ui,
                        "Listen to",
                        &mut self.settings.source,
                        &Source::ALL,
                        source_label,
                    );
                });
            Row::new("Hide after silence")
                .help("0 keeps them up.")
                .show(ui, |ui, named| {
                    changed |= number(
                        ui,
                        named,
                        &mut self.settings.hide_after_silence_s,
                        0..=3600,
                        "s",
                    );
                });
            Row::new("Save transcripts")
                .help("Keep each session to read later.")
                .show(ui, |ui, _| {
                    changed |= widgets::switch(
                        ui,
                        &mut self.settings.save_transcripts,
                        "Save transcripts",
                    )
                    .changed();
                });
        });
        widgets::group_label(ui, "Tray");
        widgets::group(ui, |ui| {
            Row::new("Hide icon after")
                .help("Moves to hidden icons when idle. 0 keeps it in view.")
                .show(ui, |ui, named| {
                    changed |= number(
                        ui,
                        named,
                        &mut self.settings.tray_idle_minutes,
                        0..=1440,
                        "min",
                    );
                });
        });
        changed
    }

    // Deleting the model in use: the server could not load it any more, so back to Whisper,
    // and the page says so.
    fn forget_canary(&mut self) -> bool {
        if self.speech.model != SpeechModel::Canary {
            return false;
        }
        self.speech = Speech {
            model: SpeechModel::Whisper,
            ..self.speech
        }
        .normalised();
        self.speech_note = Some("Canary was deleted, so the speech model is Whisper again.".into());
        true
    }

    fn model_state(&self, tool: Tool) -> Arc<Mutex<ModelState>> {
        match tool {
            Tool::Cleanup => Arc::clone(&self.model),
            Tool::Canary => Arc::clone(&self.canary_model),
        }
    }

    fn refresh_model(&self, ctx: &egui::Context, tool: Tool) {
        let model = self.model_state(tool);
        let ctx = ctx.clone();
        std::thread::spawn(move || {
            let status = model::status(tool);
            model.lock().unwrap().status = Some(status);
            ctx.request_repaint();
        });
    }

    fn run_model(&self, ctx: &egui::Context, tool: Tool, action: &'static str) {
        let model = self.model_state(tool);
        let ctx = ctx.clone();
        {
            let mut state = model.lock().unwrap();
            state.busy = true;
            state.error.clear();
            if action == "fetch" {
                state.fetching = Some((0, 0));
            }
        }
        std::thread::spawn(move || {
            let result = model::run(tool, action, |line| {
                if let Line::Progress { done, total } = line {
                    model.lock().unwrap().fetching = Some((done, total));
                    ctx.request_repaint();
                }
            });
            let status = model::status(tool);
            let mut state = model.lock().unwrap();
            state.busy = false;
            state.fetching = None;
            state.error = result.err().unwrap_or_default();
            state.status = Some(status);
            drop(state);
            ctx.request_repaint();
        });
    }

    fn cleanup(&mut self, ui: &mut egui::Ui) {
        let state = self.model.lock().unwrap().clone();
        let known = match &state.status {
            Some(Ok(status)) => Some(status.clone()),
            _ => None,
        };
        ui.add_space(12.0);
        widgets::banner(
            ui,
            Tone::Warn,
            "New and tuned on one microphone. It may miss a quiet word, or leave text as you \
             said it when the model's answer does not check out.",
            |_| {},
        );
        widgets::group_label(ui, "Dictation cleanup");
        widgets::group(ui, |ui| {
            let missing = self.cleanup.enabled && known.as_ref().is_some_and(|s| !s.present);
            let (help, tone) = if missing {
                (
                    "Download the model below first; until then the raw text is typed.",
                    Tone::Warn,
                )
            } else {
                (
                    "Drops ums and acts on “scratch that”, on this machine. It only ever \
                     deletes words.",
                    Tone::Neutral,
                )
            };
            Row::new("Clean up dictation")
                .help(help)
                .tone(tone)
                .show(ui, |ui, _| {
                    self.cleanup_dirty |=
                        widgets::switch(ui, &mut self.cleanup.enabled, "Clean up dictation")
                            .changed();
                });
            let off = !self.cleanup.enabled;
            ui.add_enabled_ui(!off, |ui| {
                Row::new("Run")
                    .help("About two seconds each. Smart skips text with nothing to fix.")
                    .show(ui, |ui, named| {
                        self.cleanup_dirty |= combo(
                            ui,
                            "cleanup-mode",
                            named,
                            &mut self.cleanup.mode,
                            &CleanupMode::ALL,
                            mode_label,
                        );
                    });
                Row::new("Time limit")
                    .help("Then the raw text is typed. The first run also loads the model.")
                    .show(ui, |ui, named| {
                        widgets::small(ui, "s");
                        let slider = ui.add(
                            egui::DragValue::new(&mut self.cleanup.budget_s)
                                .range(0.5..=30.0)
                                .speed(0.1)
                                .fixed_decimals(1),
                        );
                        self.cleanup_dirty |= named(slider)
                            .on_hover_cursor(egui::CursorIcon::Text)
                            .changed();
                    });
            });
        });

        match &state.status {
            None => {
                widgets::group_label(ui, "Model");
                widgets::group(ui, |ui| {
                    Row::new("Download").show(ui, |ui, _| {
                        a11y::live(&widgets::chip(ui, ChipKind::Neutral, "Checking…"));
                    });
                });
            }
            Some(Err(why)) => {
                widgets::group_label(ui, "Model");
                widgets::group(ui, |ui| {
                    Row::new("Download")
                        .help(why)
                        .tone(Tone::Error)
                        .show(ui, |_, _| {});
                });
            }
            Some(Ok(status)) => {
                self.model_rows(ui, Tool::Cleanup, status, &state);
            }
        }
    }

    fn model_rows(
        &mut self,
        ui: &mut egui::Ui,
        tool: Tool,
        status: &Status,
        state: &ModelState,
    ) -> bool {
        let mut speech_changed = false;
        let p = theme::palette(ui.ctx());
        let mut confirm = match tool {
            Tool::Cleanup => self.confirm_delete,
            Tool::Canary => self.confirm_delete_canary,
        };
        widgets::group_label(ui, &status.label);
        widgets::group(ui, |ui| {
            let row = Row::new("Download").help(match tool {
                Tool::Cleanup => "Checked against pinned sha256s.",
                Tool::Canary => {
                    "INT8 for the NPU, fp16 for CPU and GPU, checked against pinned sha256s. \
                     CC-BY-4.0, by NVIDIA."
                }
            });
            let row = if confirm { row.tint(p.error_tint) } else { row };
            row.show(ui, |ui, _| {
                if confirm {
                    if ui
                        .add(Button::destructive("Delete the model").small())
                        .clicked()
                    {
                        confirm = false;
                        if tool == Tool::Canary {
                            speech_changed |= self.forget_canary();
                        }
                        self.run_model(ui.ctx(), tool, "delete");
                    }
                    if ui.add(Button::ghost("Cancel").small()).clicked() {
                        confirm = false;
                    }
                    return;
                }
                let deletable = status.present && !state.busy;
                if ui
                    .add_enabled(deletable, Button::secondary("Delete…"))
                    .clicked()
                {
                    confirm = true;
                }
                let label = if status.present { "Verify" } else { "Download" };
                let button = if status.present {
                    Button::secondary(label)
                } else {
                    Button::primary(label)
                };
                if ui.add_enabled(!state.busy, button).clicked() {
                    confirm = false;
                    self.run_model(ui.ctx(), tool, "fetch");
                }
                let shown = if let Some((done, total)) = state.fetching {
                    let fraction = done as f32 / total.max(1) as f32;
                    let text = if total == 0 {
                        "Starting…".to_owned()
                    } else {
                        format!("{} of {}", model::size(done), model::size(total))
                    };
                    let shown = widgets::small(ui, text);
                    widgets::progress(ui, fraction, 120.0);
                    shown
                } else if state.busy {
                    widgets::chip(ui, ChipKind::Neutral, "Deleting…")
                } else if status.present {
                    widgets::chip(
                        ui,
                        ChipKind::Ok,
                        &format!("Downloaded, {}", model::size(status.bytes)),
                    )
                } else {
                    widgets::chip(
                        ui,
                        ChipKind::Warn,
                        &format!("Not downloaded ({})", model::size(status.download_bytes)),
                    )
                };
                a11y::live(&shown);
            });
            if !state.error.is_empty() {
                Row::new("Last attempt")
                    .help(&format!("The last attempt failed: {}", state.error))
                    .tone(Tone::Error)
                    .show(ui, |_, _| {});
            }
            if tool == Tool::Canary {
                return;
            }
            Row::new("Compiled cache")
                .help("Built on first load. Deleting the model clears it too.")
                .show(ui, |ui, _| {
                    ui.label(model::size(status.cache_bytes));
                });
        });
        match tool {
            Tool::Cleanup => self.confirm_delete = confirm,
            Tool::Canary => self.confirm_delete_canary = confirm,
        }
        speech_changed
    }

    fn refresh_model_ready(&self, ctx: &egui::Context) {
        let model_ready = Arc::clone(&self.model_ready);
        let ctx = ctx.clone();
        std::thread::spawn(move || {
            let ready = sessions::list().ok().map(|listing| listing.model_ready);
            *model_ready.lock().unwrap() = ready;
            ctx.request_repaint();
        });
    }

    fn sessions(&mut self, ui: &mut egui::Ui) {
        let model_ready = *self.model_ready.lock().unwrap();
        widgets::group_label(ui, "Summaries");
        widgets::group(ui, |ui| {
            let (help, tone) = if model_ready == Some(false) {
                (
                    "The model isn't downloaded yet; get it on the Cleanup page.",
                    Tone::Warn,
                )
            } else {
                (
                    "After each saved session, a small model on this machine writes a short \
                     summary with time stamps. Nothing leaves the machine.",
                    Tone::Neutral,
                )
            };
            Row::new("Summarise automatically")
                .help(help)
                .tone(tone)
                .show(ui, |ui, _| {
                    self.summaries_dirty |= widgets::switch(
                        ui,
                        &mut self.summaries.auto,
                        "Summarise saved sessions automatically",
                    )
                    .changed();
                    if model_ready == Some(false)
                        && ui.add(Button::secondary("Go to Cleanup").small()).clicked()
                    {
                        self.page = Page::Cleanup;
                    }
                });
        });
        widgets::group_label(ui, "Saved sessions");
        widgets::group(ui, |ui| {
            let (help, tone) = if self.settings.save_transcripts {
                (
                    "Transcripts and summaries open in a window of their own.",
                    Tone::Neutral,
                )
            } else {
                (
                    "Saving transcripts is off (Captions page), so new sessions aren't kept.",
                    Tone::Warn,
                )
            };
            Row::new("Sessions window")
                .help(help)
                .tone(tone)
                .show(ui, |ui, _| {
                    if ui.add(Button::secondary("Open saved sessions")).clicked() {
                        forward::open_sessions();
                    }
                });
            let (help, tone) = match &self.app_menu_error {
                Some(err) => (format!("Could not change the app menu: {err}"), Tone::Error),
                None => (
                    "Adds vinoWhisper Sessions to the app list, to open saved sessions \
                     without opening Settings first."
                        .to_owned(),
                    Tone::Neutral,
                ),
            };
            Row::new("App menu")
                .help(&help)
                .tone(tone)
                .show(ui, |ui, _| {
                    let mut shown = self.in_app_menu;
                    if widgets::switch(ui, &mut shown, "Show saved sessions in the app menu")
                        .changed()
                    {
                        self.set_app_menu(shown);
                    }
                });
        });
    }

    fn set_app_menu(&mut self, show: bool) {
        // with_file_name, not the exe itself: after an upgrade that reads "<path> (deleted)".
        let gui = std::env::current_exe()
            .map(|exe| exe.with_file_name("vinowhisper-gui"))
            .unwrap_or_else(|_| PathBuf::from("vinowhisper-gui"));
        match vinowhisper_settings::show_sessions_entry_in(&self.apps_data, show, &gui) {
            Ok(()) => {
                self.in_app_menu = show;
                self.app_menu_error = None;
                refresh_app_menu(self.apps_data.join("applications"));
            }
            Err(err) => self.app_menu_error = Some(err.to_string()),
        }
    }

    fn speech(&mut self, ui: &mut egui::Ui) -> bool {
        let mut changed = false;
        if let Some(notice) = self.notice.clone() {
            ui.add_space(12.0);
            if widgets::notice(ui, Tone::Error, &notice) {
                self.notice = None;
            }
        }
        if let Some(note) = self.speech_note.clone() {
            ui.add_space(12.0);
            if widgets::notice(ui, Tone::Info, &note) {
                self.speech_note = None;
            }
        }
        widgets::group_label(ui, "Recognition");
        let model = self.speech.model;
        let canary_state = self.canary_model.lock().unwrap().clone();
        // The download's own status once known; until then, what is on disk.
        let canary = match &canary_state.status {
            Some(Ok(status)) => status.present,
            _ => self.canary,
        };
        let multilingual = match model {
            SpeechModel::Whisper => self.multilingual,
            SpeechModel::Canary => true,
        };
        widgets::group(ui, |ui| {
            Row::new("Speech model")
                .help(model_help(model, model != SpeechModel::Canary || canary))
                .show(ui, |ui, named| {
                    let before = self.speech.model;
                    let shown = widgets::combo(
                        ui,
                        "speech-model",
                        model_label(before).to_owned(),
                        COMBO_WIDTH,
                    )
                    .show_ui(ui, |ui| {
                        for option in SpeechModel::ALL {
                            let installed = option != SpeechModel::Canary || canary;
                            let label = if installed {
                                model_label(option).to_owned()
                            } else {
                                format!("{} (not installed)", model_label(option))
                            };
                            ui.add_enabled_ui(installed, |ui| {
                                ui.selectable_value(&mut self.speech.model, option, label);
                            });
                        }
                    });
                    named(shown.response);
                    if self.speech.model != before {
                        let wanted = self.speech;
                        self.speech = self.speech.normalised();
                        self.speech_note = (self.speech.language != wanted.language)
                            .then(|| model_switch_note(wanted));
                        changed = true;
                    }
                });
            Row::new("Language")
                .help(match model {
                    SpeechModel::Whisper if multilingual => {
                        "Detect automatically picks per sentence. Switching reloads the model."
                    }
                    SpeechModel::Whisper => {
                        "Only English is installed. Run vinowhisper-setup --language auto to add \
                         the others."
                    }
                    SpeechModel::Canary => {
                        "Detect automatically translates whatever is spoken into English. To \
                         caption in another language, name it. Switching reloads the model."
                    }
                })
                .show(ui, |ui, named| {
                    ui.add_enabled_ui(multilingual, |ui| {
                        if combo(
                            ui,
                            "language",
                            named,
                            &mut self.speech.language,
                            self.speech.model.languages(),
                            language_label,
                        ) {
                            self.speech = self.speech.normalised();
                            changed = true;
                        }
                    });
                });
            let foreign = multilingual && self.speech.language != Language::En;
            // Canary's auto only ever translates (it has no detection to transcribe with).
            let locked = model == SpeechModel::Canary && self.speech.language == Language::Auto;
            Row::new("Translation")
                .help(if locked {
                    "Always on with Detect automatically: Canary translates whatever is spoken."
                } else if foreign {
                    "Captions arrive in English whatever was spoken."
                } else if self.speech.language == Language::En {
                    "English speech is captioned as it is. To translate, set Language to the \
                     one being spoken, or to Detect automatically."
                } else {
                    "Needs the other languages installed, as above."
                })
                .show(ui, |ui, _| {
                    ui.add_enabled_ui(foreign && !locked, |ui| {
                        let mut translate = self.speech.task == Task::Translate;
                        if widgets::switch(ui, &mut translate, "Translate to English").changed() {
                            self.speech.task = if translate {
                                Task::Translate
                            } else {
                                Task::Transcribe
                            };
                            changed = true;
                        }
                    });
                });
        });
        if let Some(Ok(status)) = &canary_state.status {
            changed |= self.model_rows(ui, Tool::Canary, status, &canary_state);
        }
        changed
    }
}

// Plasma and GNOME watch the directory anyway; the cache is for the rest. Off the UI thread.
fn refresh_app_menu(dir: PathBuf) {
    std::thread::spawn(move || {
        let _ = std::process::Command::new("update-desktop-database")
            .arg(dir)
            .stdin(std::process::Stdio::null())
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .status();
    });
}

// The caption box as the overlay draws it, on a stand-in screen.
fn caption_preview(ui: &mut egui::Ui, position: Position, size: TextSize) {
    let p = theme::palette(ui.ctx());
    let height = 132.0;
    let (rect, response) = ui.allocate_exact_size(
        egui::vec2(ui.available_width(), height),
        egui::Sense::hover(),
    );
    a11y::decorative(ui, response.id);
    if !ui.is_rect_visible(rect) {
        return;
    }
    let painter = ui.painter();
    painter.rect(
        rect,
        8.0,
        p.field,
        egui::Stroke::new(1.0_f32, p.rule),
        egui::StrokeKind::Inside,
    );
    for (y, w) in [(12.0, 120.0), (28.0, 200.0)] {
        painter.rect_filled(
            egui::Rect::from_min_size(rect.min + egui::vec2(14.0, y), egui::vec2(w, 8.0)),
            4.0,
            p.pressed,
        );
    }
    let text_size = match size {
        TextSize::Small => 12.0,
        TextSize::Medium => 14.0,
        TextSize::Large => 17.0,
    };
    let galley = painter.layout_no_wrap(
        "…and that's why the release moved to Thursday.".to_owned(),
        egui::FontId::proportional(text_size),
        egui::Color32::PLACEHOLDER,
    );
    let box_w = (galley.size().x + 28.0).min(rect.width() * 0.72);
    let box_h = galley.size().y + 16.0;
    let y = match position {
        Position::Bottom => rect.bottom() - 14.0 - box_h,
        Position::Top => rect.top() + 14.0,
    };
    let caption = egui::Rect::from_min_size(
        egui::pos2(rect.center().x - box_w / 2.0, y),
        egui::vec2(box_w, box_h),
    );
    painter.rect_filled(
        caption,
        10.0,
        egui::Color32::from_rgba_unmultiplied(14, 14, 16, 224),
    );
    let clipped = painter.with_clip_rect(caption.shrink2(egui::vec2(14.0, 0.0)));
    clipped.galley(
        egui::pos2(
            caption.left() + 14.0,
            caption.center().y - galley.size().y / 2.0,
        ),
        galley,
        egui::Color32::from_rgb(0xfa, 0xfa, 0xfa),
    );
}

fn keys(ui: &mut egui::Ui, trigger: &str) {
    if trigger.is_empty() {
        widgets::muted(ui, "Not bound");
        return;
    }
    widgets::keys(ui, trigger);
}

fn tab_name(page: Page) -> String {
    if page.beta() {
        format!("{} (beta)", page.title())
    } else {
        page.title().to_owned()
    }
}

impl eframe::App for SettingsWindow {
    fn on_exit(&mut self, _gl: Option<&eframe::glow::Context>) {
        if self.cleanup_dirty {
            self.cleanup.save();
        }
        if self.summaries_dirty {
            self.summaries.save();
        }
    }

    fn ui(&mut self, ui: &mut egui::Ui, _frame: &mut eframe::Frame) {
        let ctx = &ui.ctx().clone();
        let focused = ctx.input(|input| input.focused);
        if focused && !self.focused {
            // The desktop's light/dark switch may have flipped while we were in the background.
            theme::follow_desktop(ctx);
            self.kde_header = theme::kde_header();
            self.follow_text_scale(ctx, theme::desktop_text_scale());
        }
        let regained = focused && !self.focused;
        self.focused = focused;
        // The tray's state names the chip in the sidebar, so it is asked on any first page.
        if self.shown.is_none()
            || (self.page == Page::Shortcuts && (regained || self.shown != Some(Page::Shortcuts)))
        {
            self.ask_shortcuts(ctx, Duration::ZERO);
        }
        if self.shown != Some(Page::Cleanup) && self.page == Page::Cleanup {
            self.refresh_model(ctx, Tool::Cleanup);
        }
        if self.shown != Some(Page::Speech) && self.page == Page::Speech {
            self.refresh_model(ctx, Tool::Canary);
        }
        if self.page == Page::Sessions && (regained || self.shown != Some(Page::Sessions)) {
            self.refresh_model_ready(ctx);
        }
        self.shown = Some(self.page);
        if let Some(reason) = self.refused.lock().unwrap().take() {
            // The tray put the file back; show what is really in use.
            self.speech = Speech::load();
            self.notice = Some(reason);
        }
        let (saved_settings, saved_speech) = self.show(ui);

        if self.cleanup_dirty {
            self.cleanup_dirty = false;
            self.cleanup.save();
        }
        if self.summaries_dirty {
            self.summaries_dirty = false;
            self.summaries.save();
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
    // effect but the buttons', is left to `ui`, so tests can drive this alone.
    fn show(&mut self, ui: &mut egui::Ui) -> (bool, bool) {
        let ctx = &ui.ctx().clone();
        ctx.accesskit_node_builder(egui::accesskit_root_id(), |node| node.set_label(TITLE));
        let p = theme::palette(ctx);
        let header = theme::header(ctx, self.kde_header.as_ref(), self.focused);
        let wide = ctx.content_rect().width() >= widgets::SIDEBAR_FROM;
        let mut saved_settings = false;
        let mut saved_speech = false;
        let tray = match &*self.shortcuts.lock().unwrap() {
            Shortcuts::Stopped => Some((ChipKind::Warn, "Tray not running")),
            Shortcuts::Bound { .. } | Shortcuts::Unavailable => {
                Some((ChipKind::Neutral, "Running in the tray"))
            }
            Shortcuts::Asking | Shortcuts::Pending | Shortcuts::Unknown(_) => None,
        };

        if wide {
            egui::Panel::left("pages")
                .resizable(false)
                .show_separator_line(false)
                .exact_size(widgets::SIDEBAR_WIDTH)
                .frame(widgets::sidebar_frame(p))
                .show(ui, |ui| {
                    ui.spacing_mut().item_spacing.y = 2.0;
                    ui.horizontal(|ui| {
                        ui.add_space(8.0);
                        vino_ui::logo::whisper(ui, 28.0);
                        ui.vertical(|ui| {
                            ui.spacing_mut().item_spacing.y = 0.0;
                            ui.label(
                                RichText::new("vinoWhisper")
                                    .size(15.0)
                                    .family(theme::bold()),
                            );
                            widgets::small(ui, "Settings");
                        });
                    });
                    ui.add_space(14.0);
                    a11y::tab_list(ui, "Pages");
                    for page in Page::ALL {
                        if widgets::nav_item(
                            ui,
                            page.icon(),
                            page.title(),
                            &tab_name(page),
                            self.page == page,
                            page.beta().then_some("Beta"),
                        )
                        .clicked()
                        {
                            self.page = page;
                        }
                    }
                    if let Some((kind, text)) = tray {
                        ui.with_layout(Layout::bottom_up(Align::Min), |ui| {
                            ui.add_space(2.0);
                            ui.horizontal(|ui| {
                                ui.add_space(4.0);
                                a11y::live(&widgets::chip(ui, kind, text));
                            });
                        });
                    }
                    widgets::panel_rule(ui, widgets::Edge::Left);
                });
        } else {
            egui::Panel::top("header")
                .show_separator_line(false)
                .frame(widgets::header_frame(header, p).inner_margin(egui::Margin {
                    left: 16,
                    right: 16,
                    top: 8,
                    bottom: 0,
                }))
                .show(ui, |ui| {
                    ui.horizontal(|ui| {
                        vino_ui::logo::whisper(ui, 22.0);
                        ui.label(
                            RichText::new("vinoWhisper")
                                .size(15.0)
                                .family(theme::bold())
                                .color(header.text),
                        );
                    });
                    // Wrapped: at the desktop's largest text the tabs don't fit on one line.
                    ui.horizontal_wrapped(|ui| {
                        ui.spacing_mut().item_spacing = egui::vec2(18.0, 0.0);
                        a11y::tab_list(ui, "Pages");
                        for page in Page::ALL {
                            if widgets::tab(ui, &tab_name(page), self.page == page, true, header)
                                .clicked()
                            {
                                self.page = page;
                            }
                        }
                    });
                });
        }

        egui::CentralPanel::default()
            .frame(
                egui::Frame::NONE
                    .fill(p.bg)
                    // No right margin: the scroll bar belongs on the window's edge, and the
                    // content keeps its own gap from it below.
                    .inner_margin(egui::Margin {
                        left: if wide { widgets::PAGE_INSET as i8 } else { 16 },
                        right: 0,
                        top: if wide { 20 } else { 12 },
                        bottom: 0,
                    }),
            )
            .show(ui, |ui| {
                widgets::page_scroll(ui, self.page.title(), widgets::CONTENT_MAX, |ui| {
                    ui.horizontal(|ui| {
                        ui.spacing_mut().item_spacing.x = 10.0;
                        widgets::title(ui, self.page.title());
                        if self.page.beta() {
                            let tag = widgets::tag(ui, Tone::Warn, "Beta");
                            a11y::decorative(ui, tag.id);
                        }
                    });
                    match self.page {
                        Page::Shortcuts => self.shortcuts(ui),
                        Page::Captions => saved_settings = self.captions(ui),
                        Page::Speech => saved_speech = self.speech(ui),
                        Page::Cleanup => self.cleanup(ui),
                        Page::Sessions => self.sessions(ui),
                        Page::About => vino_ui::about::page_with_updates(ui, &mut self.updater),
                    }
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
            .with_inner_size([820.0, 680.0])
            .with_min_inner_size([340.0, 320.0]),
        // With vsync, a swap waits on a frame callback KWin holds back from a window it
        // considers hidden, and the window stops answering pings. It only redraws on input.
        // eframe 0.37 skips the blocking swap on Wayland itself (egui#5136); this keeps X11 too.
        glow_options: eframe::egui_glow::GlowConfiguration {
            vsync: false,
            ..Default::default()
        },
        ..Default::default()
    };
    let result = eframe::run_native(
        TITLE,
        options,
        Box::new(move |cc| {
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
    use eframe::egui::accesskit::{Live, Role, Toggled};
    use egui_kittest::Harness;
    use egui_kittest::kittest::{NodeT, Queryable};

    // No file is read or written and nothing is spawned: `show` is the window minus its
    // side effects, and the tests never press the buttons that run the tray or vinowhisper-cleanup.
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
        window.apps_data = std::env::temp_dir().join(format!(
            "vinowhisper-gui-test-{}-{:?}",
            std::process::id(),
            std::thread::current().id()
        ));
        window.model.lock().unwrap().status = Some(Ok(Status {
            label: "SmolLM3-3B".into(),
            download_bytes: 1_702_000_000,
            ..Status::default()
        }));
        *window.shortcuts.lock().unwrap() = Shortcuts::Bound {
            captions: "Meta+Alt+C".into(),
            dictate: "Meta+H".into(),
            configurable: true,
        };
        Harness::builder().with_size((width, 740.0)).build_ui_state(
            |ui, under: &mut Under| {
                let ctx = &ui.ctx().clone();
                // Fonts only bind at the next pass, and the bold family is not stock.
                if !under.styled {
                    theme::apply(ctx);
                    under.styled = true;
                    ctx.request_repaint();
                    return;
                }
                let (settings, speech) = under.window.show(ui);
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
            for label in ["Position", "Text size", "Listen to"] {
                harness.get_by_role_and_label(Role::RadioGroup, label);
            }
            for label in ["Hide after silence", "Hide icon after"] {
                harness.get_by_role_and_label(Role::SpinButton, label);
            }
            harness.get_by_role_and_label(Role::Switch, "Save transcripts");
            for title in ["Overlay", "Listening", "Tray"] {
                harness.get_by_role_and_label(Role::Heading, title);
            }
        }
    }

    #[test]
    fn a_segmented_control_can_be_changed_and_says_what_it_is_set_to() {
        let mut harness = harness(Page::Captions, WIDE);
        harness.run();
        let bottom = harness.get_by_role_and_label(Role::RadioButton, "Bottom");
        assert_eq!(bottom.accesskit_node().toggled(), Some(Toggled::True));
        harness
            .get_by_role_and_label(Role::RadioButton, "Top")
            .click();
        harness.run();
        assert_eq!(harness.state().window.settings.position, Position::Top);
        assert!(harness.state().saved.0);
        let top = harness.get_by_role_and_label(Role::RadioButton, "Top");
        assert_eq!(top.accesskit_node().toggled(), Some(Toggled::True));
    }

    #[test]
    fn escape_closes_an_open_combo_box_without_changing_it() {
        let mut harness = harness(Page::Cleanup, WIDE);
        harness.state_mut().window.cleanup.enabled = true;
        harness.run();
        harness.get_by_role_and_label(Role::ComboBox, "Run").click();
        harness.run();
        assert!(harness.query_by_label("Every dictation").is_some());
        harness.key_press(egui::Key::Escape);
        harness.run();
        assert!(harness.query_by_label("Every dictation").is_none());
        assert_eq!(harness.state().window.cleanup.mode, CleanupMode::Smart);
    }

    #[test]
    fn a_switch_reports_and_flips_its_state() {
        let mut harness = harness(Page::Captions, WIDE);
        harness.run();
        let switch = harness.get_by_role_and_label(Role::Switch, "Save transcripts");
        let before = switch.accesskit_node().toggled();
        switch.focus();
        harness.run();
        harness.key_press(egui::Key::Space);
        harness.run();
        assert!(harness.state().window.settings.save_transcripts);
        assert!(harness.state().saved.0);
        let after = harness
            .get_by_role_and_label(Role::Switch, "Save transcripts")
            .accesskit_node()
            .toggled();
        assert_ne!(before, after);
    }

    #[test]
    fn the_speech_page_names_its_controls() {
        let mut harness = harness(Page::Speech, WIDE);
        harness.run();
        harness.get_by_role_and_label(Role::ComboBox, "Speech model");
        harness.get_by_role_and_label(Role::ComboBox, "Language");
        harness.get_by_role_and_label(Role::Switch, "Translate to English");
    }

    #[test]
    fn canary_says_how_to_install_it_until_it_is_there() {
        let mut harness = harness(Page::Speech, WIDE);
        harness.run();
        harness.get_by_label_contains("Whisper small, 0.9 GB");
        harness.state_mut().window.speech.model = SpeechModel::Canary;
        harness.run();
        harness.get_by_label_contains("not downloaded yet: use Download below");
        harness.state_mut().window.canary = true;
        harness.run();
        harness.get_by_label_contains("25 European languages");
        assert!(
            harness
                .query_by_label_contains("not downloaded yet")
                .is_none()
        );
    }

    #[test]
    fn canary_offers_its_own_languages_and_detection_to_translate() {
        let mut harness = harness(Page::Speech, WIDE);
        harness.state_mut().window.canary = true;
        harness.state_mut().window.speech.model = SpeechModel::Canary;
        harness.run();
        harness
            .get_by_role_and_label(Role::ComboBox, "Language")
            .click();
        harness.run();
        harness.get_by_label("Italian");
        harness.get_by_label("Detect automatically");
    }

    #[test]
    fn switching_to_canary_keeps_auto_translate() {
        let mut harness = harness(Page::Speech, WIDE);
        let window = &mut harness.state_mut().window;
        window.canary = true;
        window.speech.language = Language::Auto;
        window.speech.task = Task::Translate;
        harness.run();
        harness
            .get_by_role_and_label(Role::ComboBox, "Speech model")
            .click();
        harness.run();
        harness.get_by_label("Canary 1B v2").click();
        harness.run();
        let speech = harness.state().window.speech;
        assert_eq!(
            (speech.model, speech.language, speech.task),
            (SpeechModel::Canary, Language::Auto, Task::Translate)
        );
        assert!(harness.state().window.speech_note.is_none());
        let switch = harness.get_by_role_and_label(Role::Switch, "Translate to English");
        assert!(
            switch.accesskit_node().is_disabled(),
            "locked on for Canary's auto"
        );
        assert_eq!(
            switch.accesskit_node().toggled(),
            Some(egui::accesskit::Toggled::True)
        );
    }

    #[test]
    fn a_model_switch_that_drops_the_language_says_so() {
        let mut harness = harness(Page::Speech, WIDE);
        let window = &mut harness.state_mut().window;
        window.canary = true;
        window.speech.language = Language::Auto;
        harness.run();
        harness
            .get_by_role_and_label(Role::ComboBox, "Speech model")
            .click();
        harness.run();
        harness.get_by_label("Canary 1B v2").click();
        harness.run();
        assert_eq!(harness.state().window.speech.language, Language::En);
        harness.get_by_label_contains("so Language is now English");
    }

    fn canary_status(present: bool) -> Status {
        Status {
            label: "Canary 1B v2".into(),
            download_bytes: 972_131_242,
            present,
            bytes: if present { 972_131_242 } else { 0 },
            cache_bytes: 0,
        }
    }

    #[test]
    fn canary_can_be_downloaded_from_the_speech_page() {
        let mut harness = harness(Page::Speech, WIDE);
        harness
            .state_mut()
            .window
            .canary_model
            .lock()
            .unwrap()
            .status = Some(Ok(canary_status(false)));
        harness.run();
        harness.get_by_label("Not downloaded (972 MB)");
        harness.get_by_role_and_label(Role::Button, "Download");
        harness
            .get_by_role_and_label(Role::ComboBox, "Speech model")
            .click();
        harness.run();
        harness.get_by_label("Canary 1B v2 (not installed)");
    }

    #[test]
    fn a_downloaded_canary_can_be_picked() {
        let mut harness = harness(Page::Speech, WIDE);
        harness
            .state_mut()
            .window
            .canary_model
            .lock()
            .unwrap()
            .status = Some(Ok(canary_status(true)));
        harness.run();
        harness.get_by_label("Downloaded, 972 MB");
        harness
            .get_by_role_and_label(Role::ComboBox, "Speech model")
            .click();
        harness.run();
        // The download group is headed "Canary 1B v2" too; the option is the one that isn't a label.
        harness
            .get_all_by_label("Canary 1B v2")
            .find(|node| node.accesskit_node().role() != Role::Label)
            .unwrap()
            .click();
        harness.run();
        assert_eq!(harness.state().window.speech.model, SpeechModel::Canary);
        assert!(harness.state().saved.1, "the switch is saved");
    }

    #[test]
    fn deleting_the_canary_in_use_goes_back_to_whisper() {
        let mut window = SettingsWindow::with(
            Some(Page::Speech),
            Settings::default(),
            Speech {
                language: Language::Auto,
                task: Task::Translate,
                model: SpeechModel::Canary,
            },
            Cleanup::default(),
        );
        assert!(window.forget_canary());
        assert_eq!(
            window.speech,
            Speech {
                language: Language::Auto,
                task: Task::Translate,
                model: SpeechModel::Whisper,
            }
        );
        assert!(window.speech_note.is_some());
        assert!(!window.forget_canary(), "Whisper in use: nothing to switch");
    }

    #[test]
    fn translation_says_why_it_is_off_for_english() {
        let mut harness = harness(Page::Speech, WIDE);
        harness.run();
        let switch = harness.get_by_role_and_label(Role::Switch, "Translate to English");
        assert!(switch.accesskit_node().is_disabled());
        harness.get_by_label_contains("set Language to the one being spoken");
        harness.state_mut().window.speech.language = Language::Fr;
        harness.run();
        let switch = harness.get_by_role_and_label(Role::Switch, "Translate to English");
        assert!(!switch.accesskit_node().is_disabled());
        harness.get_by_label("Captions arrive in English whatever was spoken.");
    }

    #[test]
    fn the_cleanup_page_names_its_fields_and_asks_for_no_key() {
        let mut harness = harness(Page::Cleanup, WIDE);
        harness.state_mut().window.cleanup.enabled = true;
        harness.run();
        harness.get_by_role_and_label(Role::Switch, "Clean up dictation");
        harness.get_by_role_and_label(Role::ComboBox, "Run");
        harness.get_by_role_and_label(Role::SpinButton, "Time limit");
        for gone in ["Address", "Model"] {
            assert!(
                harness
                    .query_by_role_and_label(Role::TextInput, gone)
                    .is_none(),
                "{gone}"
            );
        }
        assert!(
            harness
                .query_by_role_and_label(Role::PasswordInput, "New key")
                .is_none()
        );
        harness.get_by_label("Not downloaded (1.7 GB)");
        harness.get_by_label("Download the model below first; until then the raw text is typed.");
        let delete = harness.get_by_role_and_label(Role::Button, "Delete…");
        assert!(delete.accesskit_node().is_disabled(), "nothing to delete");
        harness.get_by_role_and_label(Role::Button, "Download");
        harness.get_by_role_and_label(Role::Heading, "Cleanup");
    }

    #[test]
    fn a_downloaded_model_offers_verify_and_a_confirmed_delete() {
        let mut harness = harness(Page::Cleanup, WIDE);
        harness.state_mut().window.model.lock().unwrap().status = Some(Ok(Status {
            label: "SmolLM3-3B".into(),
            download_bytes: 1_702_000_000,
            present: true,
            bytes: 1_701_000_000,
            cache_bytes: 2_100_000_000,
        }));
        harness.run();
        harness.get_by_label("Downloaded, 1.7 GB");
        harness.get_by_label("2.1 GB");
        harness.get_by_role_and_label(Role::Button, "Verify");
        harness
            .get_by_role_and_label(Role::Button, "Delete…")
            .click();
        harness.run();
        assert!(harness.state().window.confirm_delete);
        harness.get_by_role_and_label(Role::Button, "Delete the model");
        harness
            .get_by_role_and_label(Role::Button, "Cancel")
            .click();
        harness.run();
        assert!(!harness.state().window.confirm_delete);
    }

    #[test]
    fn a_download_in_progress_shows_how_far_it_has_got() {
        let mut harness = harness(Page::Cleanup, WIDE);
        {
            let state = &mut harness.state_mut().window.model;
            let mut state = state.lock().unwrap();
            state.busy = true;
            state.fetching = Some((400_000_000, 1_702_000_000));
        }
        harness.run();
        harness.get_by_label("400 MB of 1.7 GB");
        let download = harness.get_by_role_and_label(Role::Button, "Download");
        assert!(
            download.accesskit_node().is_disabled(),
            "one fetch at a time"
        );
    }

    #[test]
    fn a_failed_fetch_says_why() {
        let mut harness = harness(Page::Cleanup, WIDE);
        harness.state_mut().window.model.lock().unwrap().error = "sha256 mismatch".into();
        harness.run();
        harness.get_by_label("The last attempt failed: sha256 mismatch");
    }

    // AccessKit had the disabled flag right all along; accesskit_atspi_common before 0.21
    // dropped it for buttons and told Orca every one was usable (#100, AccessKit#788). The
    // tree above can't see that, so hold the lock at the fixed release.
    #[test]
    fn the_atspi_bridge_is_one_that_exposes_disabled_buttons() {
        let lock = include_str!("../Cargo.lock");
        let version = lock
            .split("[[package]]")
            .find(|package| package.contains("name = \"accesskit_atspi_common\""))
            .and_then(|package| package.split("version = \"").nth(1))
            .and_then(|rest| rest.split('"').next())
            .expect("accesskit_atspi_common is locked");
        let minor: u32 = version.split('.').nth(1).unwrap().parse().unwrap();
        assert!(
            version.starts_with("0.") && minor >= 21,
            "accesskit_atspi_common {version} announces disabled buttons as usable"
        );
    }

    #[test]
    fn the_model_status_is_announced_when_it_changes() {
        let mut harness = harness(Page::Cleanup, WIDE);
        harness.run();
        let status = harness.get_by_label("Not downloaded (1.7 GB)");
        assert_eq!(status.accesskit_node().live(), Live::Polite);
    }

    #[test]
    fn fields_left_unused_by_cleanup_being_off_say_so() {
        let mut harness = harness(Page::Cleanup, WIDE);
        harness.run();
        assert!(!harness.state().window.cleanup.enabled);
        let limit = harness.get_by_role_and_label(Role::SpinButton, "Time limit");
        assert!(limit.accesskit_node().is_disabled());
    }

    #[test]
    fn the_shortcuts_page_reads_the_keys_and_offers_change() {
        let mut harness = harness(Page::Shortcuts, WIDE);
        harness.run();
        harness.get_by_label("Meta+Alt+C");
        harness.get_by_label("Meta+H");
        harness.get_by_role_and_label(Role::Button, "Change…");
        harness.get_by_role_and_label(Role::Heading, "Shortcuts");
        harness.get_by_label("Running in the tray");
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
    fn the_sessions_page_keeps_the_setting_and_opens_the_sessions_window() {
        let mut harness = harness(Page::Sessions, WIDE);
        *harness.state_mut().window.model_ready.lock().unwrap() = Some(false);
        harness.run();
        harness.get_by_role_and_label(Role::Switch, "Summarise saved sessions automatically");
        harness.get_by_label("The model isn't downloaded yet; get it on the Cleanup page.");
        harness.get_by_role_and_label(Role::Button, "Go to Cleanup");
        harness.get_by_role_and_label(Role::Button, "Open saved sessions");
        harness.get_by_label(
            "Saving transcripts is off (Captions page), so new sessions aren't kept.",
        );
    }

    #[test]
    fn the_app_menu_switch_adds_and_removes_the_sessions_entry() {
        let mut harness = harness(Page::Sessions, WIDE);
        let data = harness.state().window.apps_data.clone();
        let _ = std::fs::remove_dir_all(&data);
        harness.run();
        let switch = "Show saved sessions in the app menu";
        assert_eq!(
            harness
                .get_by_role_and_label(Role::Switch, switch)
                .accesskit_node()
                .toggled(),
            Some(egui::accesskit::Toggled::False),
            "off until turned on"
        );
        harness.get_by_role_and_label(Role::Switch, switch).click();
        harness.run();
        assert!(vinowhisper_settings::sessions_entry_shown_in(&data));
        let entry =
            std::fs::read_to_string(vinowhisper_settings::sessions_entry_in(&data)).unwrap();
        assert!(entry.contains("vinowhisper-gui --sessions\n"));
        harness.get_by_role_and_label(Role::Switch, switch).click();
        harness.run();
        assert!(!vinowhisper_settings::sessions_entry_shown_in(&data));
        let _ = std::fs::remove_dir_all(&data);
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
        assert!(harness.ctx.content_rect().width() < widgets::SIDEBAR_FROM);
        harness.get_by_role_and_label(Role::Tab, "Cleanup (beta)");
        harness.get_by_role_and_label(Role::RadioGroup, "Position");
        harness.get_by_role_and_label(Role::Switch, "Save transcripts");
    }
}
