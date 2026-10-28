use std::path::PathBuf;
use std::process::ExitCode;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use eframe::egui::accesskit::{Live, Role};
use eframe::egui::{self, Align, Color32, FontId, Layout, RichText, Sense, Stroke};

use crate::forward::{self, Shortcuts};
use crate::model::{self, Line, ModelState, Status};
use crate::sessions::{self, Listing, Part, Piece, Target};
use crate::theme;
use vinowhisper_settings::{
    Cleanup, CleanupMode, Language, Position, Settings, Source, Speech, Summaries, Task, TextSize,
    multilingual_installed,
};

#[derive(Clone, Copy, PartialEq, Eq)]
enum Page {
    Shortcuts,
    Captions,
    Speech,
    Cleanup,
    Sessions,
}

impl Page {
    const ALL: [Page; 5] = [
        Page::Shortcuts,
        Page::Captions,
        Page::Speech,
        Page::Cleanup,
        Page::Sessions,
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

#[derive(Clone, Default)]
struct Viewer {
    target: Option<Target>,
    text: Option<Result<String, String>>,
    // Scrolled to target.at_s already; only the first frame after loading jumps.
    scrolled: bool,
}

#[derive(Clone, Default)]
struct SessionsState {
    listing: Option<Result<Listing, String>>,
    // While the server is summarising, the list is read again every REFRESH_EVERY.
    listed_at: Option<std::time::Instant>,
    refreshing: bool,
    viewer: Viewer,
    busy: bool,
    error: String,
}

const REFRESH_EVERY: Duration = Duration::from_secs(2);

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
    model: Arc<Mutex<ModelState>>,
    confirm_delete: bool,
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
    summaries: Summaries,
    summaries_dirty: bool,
    sessions: Arc<Mutex<SessionsState>>,
    confirm_delete_session: Option<PathBuf>,
}

impl SettingsWindow {
    fn new(page: Option<Page>) -> Self {
        let mut window = Self::with(page, Settings::load(), Speech::load(), Cleanup::load());
        window.summaries = Summaries::load();
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
            model: Arc::new(Mutex::new(ModelState::default())),
            confirm_delete: false,
            multilingual: false,
            focused: true,
            kde_header: None,
            text_scale: None,
            shortcuts: Arc::new(Mutex::new(Shortcuts::Asking)),
            shown: None,
            refused: Arc::new(Mutex::new(None)),
            notice: None,
            summaries: Summaries::default(),
            summaries_dirty: false,
            sessions: Arc::new(Mutex::new(SessionsState::default())),
            confirm_delete_session: None,
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

    fn refresh_model(&self, ctx: &egui::Context) {
        let model = Arc::clone(&self.model);
        let ctx = ctx.clone();
        std::thread::spawn(move || {
            let status = model::status();
            model.lock().unwrap().status = Some(status);
            ctx.request_repaint();
        });
    }

    fn run_model(&self, ctx: &egui::Context, action: &'static str) {
        let model = Arc::clone(&self.model);
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
            let result = model::run(action, |line| {
                if let Line::Progress { done, total } = line {
                    model.lock().unwrap().fetching = Some((done, total));
                    ctx.request_repaint();
                }
            });
            let status = model::status();
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
        let p = theme::palette(ui.ctx());
        let state = self.model.lock().unwrap().clone();
        let known = match &state.status {
            Some(Ok(status)) => Some(status.clone()),
            _ => None,
        };
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
            "Runs a small language model on this machine, on the NPU when there is one, to drop \
             ums, apply corrections and act on \"scratch that\" before the text is typed. Nothing \
             leaves the machine. It only ever deletes words, and any other answer is thrown away \
             and the raw text is typed. Off by default.",
            |ui, _| {
                self.cleanup_dirty |= ui
                    .checkbox(&mut self.cleanup.enabled, "Clean up dictation")
                    .changed();
                if self.cleanup.enabled && known.as_ref().is_some_and(|s| !s.present) {
                    live(
                        &ui.label(
                            RichText::new(
                                "Download the model below first; until then the raw text is typed.",
                            )
                            .color(p.muted),
                        ),
                    );
                }
            },
        );
        let off = !self.cleanup.enabled;
        ui.add_enabled_ui(!off, |ui| {
            row(
                ui,
                "Run:",
                "Each cleanup takes about two seconds. Smart skips it for text with no filler, correction or spoken edit in it, which is most of what you say.",
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
            row(
                ui,
                "Time limit:",
                "Seconds to wait before typing the raw text instead. The first cleanup after a while also loads the model.",
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
        });

        match &state.status {
            None => {
                section(ui, "Model");
                row(ui, "", "", |ui, _| {
                    live(&ui.label(RichText::new("Checking…").color(p.muted)));
                });
            }
            Some(Err(why)) => {
                section(ui, "Model");
                row(ui, "", "", |ui, _| {
                    live(&ui.label(RichText::new(why).color(ui.visuals().error_fg_color)));
                });
            }
            Some(Ok(status)) => self.model_rows(ui, status, &state),
        }
    }

    fn model_rows(&mut self, ui: &mut egui::Ui, status: &Status, state: &ModelState) {
        let p = theme::palette(ui.ctx());
        section(ui, &status.label);
        row(
            ui,
            "Download:",
            "Needed for cleanup. Checked file by file against pinned sha256s.",
            |ui, _| {
                ui.horizontal_wrapped(|ui| {
                    ui.set_min_height(ui.spacing().interact_size.y);
                    if let Some((done, total)) = state.fetching {
                        let fraction = done as f32 / total.max(1) as f32;
                        let text = if total == 0 {
                            "Starting…".to_owned()
                        } else {
                            format!("{} of {}", model::size(done), model::size(total))
                        };
                        live(
                            &ui.add(
                                egui::ProgressBar::new(fraction)
                                    .text(text)
                                    .desired_width(220.0),
                            ),
                        );
                    } else if state.busy {
                        live(&ui.label(RichText::new("Deleting…").color(p.muted)));
                    } else if status.present {
                        live(&ui.label(format!("Downloaded, {}", model::size(status.bytes))));
                    } else {
                        let color = ui.visuals().warn_fg_color;
                        live(
                            &ui.label(
                                RichText::new(format!(
                                    "Not downloaded ({})",
                                    model::size(status.download_bytes)
                                ))
                                .color(color),
                            ),
                        );
                    }
                    let label = if status.present { "Verify" } else { "Download" };
                    if ui
                        .add_enabled(!state.busy, egui::Button::new(label))
                        .clicked()
                    {
                        self.confirm_delete = false;
                        self.run_model(ui.ctx(), "fetch");
                    }
                    let deletable = status.present && !state.busy;
                    if self.confirm_delete {
                        if ui.button("Cancel").clicked() {
                            self.confirm_delete = false;
                        }
                        if ui.button("Delete the model").clicked() {
                            self.confirm_delete = false;
                            self.run_model(ui.ctx(), "delete");
                        }
                    } else if ui
                        .add_enabled(deletable, egui::Button::new("Delete…"))
                        .clicked()
                    {
                        self.confirm_delete = true;
                    }
                });
                if !state.error.is_empty() {
                    live(
                        &ui.label(
                            RichText::new(format!("The last attempt failed: {}", state.error))
                                .color(ui.visuals().error_fg_color),
                        ),
                    );
                }
            },
        );
        row(
            ui,
            "Compiled cache:",
            "What the NPU compiled from the model on its first load. Deleting the model clears it too.",
            |ui, _| {
                ui.label(model::size(status.cache_bytes));
            },
        );
    }

    fn refresh_sessions(&self, ctx: &egui::Context) {
        let state = Arc::clone(&self.sessions);
        {
            let mut state = state.lock().unwrap();
            if state.refreshing {
                return;
            }
            state.refreshing = true;
        }
        let ctx = ctx.clone();
        std::thread::spawn(move || {
            let listing = sessions::list();
            let mut state = state.lock().unwrap();
            state.listing = Some(listing);
            state.listed_at = Some(std::time::Instant::now());
            state.refreshing = false;
            drop(state);
            ctx.request_repaint();
        });
    }

    fn follow_summaries(&self, ctx: &egui::Context) {
        let state = self.sessions.lock().unwrap();
        let working = matches!(&state.listing, Some(Ok(listing)) if listing.working());
        if !working {
            return;
        }
        let due = state
            .listed_at
            .is_none_or(|at| at.elapsed() >= REFRESH_EVERY);
        drop(state);
        if due {
            self.refresh_sessions(ctx);
        }
        ctx.request_repaint_after(REFRESH_EVERY);
    }

    fn open_session(&self, ctx: &egui::Context, target: Target) {
        let state = Arc::clone(&self.sessions);
        let ctx = ctx.clone();
        state.lock().unwrap().viewer = Viewer {
            target: Some(target.clone()),
            text: None,
            scrolled: false,
        };
        std::thread::spawn(move || {
            let text = sessions::show(&target.path, target.part);
            let mut state = state.lock().unwrap();
            // Only if it is still the one asked for: a quick second click wins.
            if state.viewer.target.as_ref() == Some(&target) {
                state.viewer.text = Some(text);
            }
            drop(state);
            ctx.request_repaint();
        });
    }

    fn session_action(&self, ctx: &egui::Context, action: &'static str, path: Option<PathBuf>) {
        let state = Arc::clone(&self.sessions);
        let ctx = ctx.clone();
        {
            let mut state = state.lock().unwrap();
            state.busy = true;
            state.error.clear();
        }
        std::thread::spawn(move || {
            let result = sessions::act(action, path.as_deref());
            let listing = sessions::list();
            let mut locked = state.lock().unwrap();
            locked.busy = false;
            locked.error = result.err().unwrap_or_default();
            locked.listing = Some(listing);
            locked.listed_at = Some(std::time::Instant::now());
            if action == "delete" && locked.viewer.target.as_ref().map(|t| &t.path) == path.as_ref()
            {
                locked.viewer = Viewer::default();
            }
            drop(locked);
            ctx.request_repaint();
        });
    }

    fn sessions(&mut self, ui: &mut egui::Ui) {
        let state = self.sessions.lock().unwrap().clone();
        if state.viewer.target.is_some() {
            self.session_viewer(ui, &state);
            return;
        }
        let p = theme::palette(ui.ctx());
        let listing = match &state.listing {
            Some(Ok(listing)) => Some(listing.clone()),
            _ => None,
        };
        section(ui, "Summaries");
        row(
            ui,
            "Summarise:",
            "After each saved session ends, a small language model on this machine writes a \
             short summary with time stamps back into the transcript. It waits until captions \
             and dictation are idle, and nothing leaves the machine.",
            |ui, _| {
                self.summaries_dirty |= ui
                    .checkbox(
                        &mut self.summaries.auto,
                        "Summarise saved sessions automatically",
                    )
                    .changed();
                if listing.as_ref().is_some_and(|l| !l.model_ready) {
                    live(
                        &ui.label(
                            RichText::new(
                                "The model isn't downloaded yet; get it on the Cleanup page.",
                            )
                            .color(ui.visuals().warn_fg_color),
                        ),
                    );
                    if ui.button("Go to Cleanup").clicked() {
                        self.page = Page::Cleanup;
                    }
                }
            },
        );
        if !self.settings.save_transcripts {
            row(ui, "", "", |ui, _| {
                ui.add(
                    egui::Label::new(
                        RichText::new(
                            "Saving transcripts is off (Captions page), so new sessions aren't kept.",
                        )
                        .color(p.muted),
                    )
                    .wrap(),
                );
            });
        }
        if let Some(listing) = listing.as_ref().filter(|l| l.backfill > 0) {
            let count = listing.backfill;
            row(
                ui,
                "Older transcripts:",
                "Saved before summaries existed, as plain text. Bundling keeps their text as it is.",
                |ui, _| {
                    let label = format!(
                        "Bundle and summarise {count} transcript{}",
                        if count == 1 { "" } else { "s" }
                    );
                    if ui
                        .add_enabled(!state.busy, egui::Button::new(label))
                        .clicked()
                    {
                        self.session_action(ui.ctx(), "backfill", None);
                    }
                },
            );
        }

        section(ui, "Saved sessions");
        if !state.error.is_empty() {
            live(
                &ui.label(
                    RichText::new(format!("That didn't work: {}", state.error))
                        .color(ui.visuals().error_fg_color),
                ),
            );
        }
        match &state.listing {
            None => {
                live(&ui.label(RichText::new("Reading…").color(p.muted)));
            }
            Some(Err(why)) => {
                live(&ui.label(RichText::new(why).color(ui.visuals().error_fg_color)));
            }
            Some(Ok(listing)) if listing.sessions.is_empty() => {
                ui.label(RichText::new("No saved sessions yet.").color(p.muted));
            }
            Some(Ok(listing)) => {
                for entry in &listing.sessions {
                    self.session_card(ui, entry, listing, state.busy);
                    ui.add_space(8.0);
                }
            }
        }
    }

    fn session_card(
        &mut self,
        ui: &mut egui::Ui,
        entry: &sessions::Entry,
        listing: &Listing,
        busy: bool,
    ) {
        let p = theme::palette(ui.ctx());
        egui::Frame::NONE
            .stroke(Stroke::new(1.0_f32, p.rule))
            .corner_radius(4.0)
            .inner_margin(egui::Margin::symmetric(12, 10))
            .show(ui, |ui| {
                ui.set_width(ui.available_width());
                let mut facts = vec![entry.when()];
                facts.extend(entry.length());
                facts.extend(entry.source.clone());
                ui.label(RichText::new(facts.join(" · ")).family(theme::bold()));
                let working = listing.current.as_ref() == Some(&entry.path);
                let waiting = working || listing.queued.contains(&entry.path);
                let status = match (&entry.title, &entry.error) {
                    (_, Some(error)) => error.clone(),
                    (Some(title), None) if !waiting => title.clone(),
                    _ => listing.status(entry).to_owned(),
                };
                ui.horizontal_wrapped(|ui| {
                    if working {
                        ui.add(egui::Spinner::new());
                    }
                    live(&ui.add(egui::Label::new(RichText::new(status).color(p.muted)).wrap()));
                });
                if !entry.verified && entry.error.is_none() {
                    ui.label(
                        RichText::new("Changed since it was written (the hashes don't match).")
                            .color(ui.visuals().warn_fg_color),
                    );
                }
                ui.add_space(4.0);
                ui.horizontal_wrapped(|ui| {
                    let readable = entry.error.is_none();
                    let path = entry.path.clone();
                    if ui
                        .add_enabled(readable, egui::Button::new("Transcript"))
                        .clicked()
                    {
                        self.open_session(
                            ui.ctx(),
                            Target {
                                path: path.clone(),
                                part: Part::Transcript,
                                at_s: None,
                            },
                        );
                    }
                    if ui
                        .add_enabled(readable && entry.summary, egui::Button::new("Summary"))
                        .clicked()
                    {
                        self.open_session(
                            ui.ctx(),
                            Target {
                                path: path.clone(),
                                part: Part::Summary,
                                at_s: None,
                            },
                        );
                    }
                    let again = if entry.summary {
                        "Summarise again"
                    } else {
                        "Summarise"
                    };
                    if ui
                        .add_enabled(readable && !busy && !waiting, egui::Button::new(again))
                        .clicked()
                    {
                        self.session_action(ui.ctx(), "summarise", Some(path.clone()));
                    }
                    if entry.summary
                        && ui
                            .add_enabled(!busy, egui::Button::new("Delete summary"))
                            .clicked()
                    {
                        self.session_action(ui.ctx(), "delete-summary", Some(path.clone()));
                    }
                    if ui.button("Show in folder").clicked() {
                        sessions::show_in_folder(&path);
                    }
                    if self.confirm_delete_session.as_ref() == Some(&path) {
                        if ui.button("Cancel").clicked() {
                            self.confirm_delete_session = None;
                        }
                        if ui.button("Delete the session").clicked() {
                            self.confirm_delete_session = None;
                            self.session_action(ui.ctx(), "delete", Some(path.clone()));
                        }
                    } else if ui
                        .add_enabled(!busy, egui::Button::new("Delete…"))
                        .clicked()
                    {
                        self.confirm_delete_session = Some(path);
                    }
                });
            });
    }

    fn session_viewer(&mut self, ui: &mut egui::Ui, state: &SessionsState) {
        let p = theme::palette(ui.ctx());
        let Some(target) = state.viewer.target.clone() else {
            return;
        };
        ui.add_space(10.0);
        if ui.button("All sessions").clicked() {
            self.sessions.lock().unwrap().viewer = Viewer::default();
            self.refresh_sessions(ui.ctx());
            return;
        }
        let entry = match &state.listing {
            Some(Ok(listing)) => listing
                .sessions
                .iter()
                .find(|e| e.path == target.path)
                .cloned(),
            _ => None,
        };
        let name = entry.as_ref().map_or_else(
            || {
                target
                    .path
                    .file_name()
                    .map(|n| n.to_string_lossy().into_owned())
                    .unwrap_or_default()
            },
            sessions::Entry::when,
        );
        section(ui, &name);
        ui.horizontal(|ui| {
            for part in [Part::Transcript, Part::Summary] {
                let label = if part == Part::Transcript {
                    "Transcript"
                } else {
                    "Summary"
                };
                let enabled = part == Part::Transcript || entry.as_ref().is_none_or(|e| e.summary);
                if ui
                    .add_enabled(
                        enabled,
                        egui::Button::selectable(target.part == part, label),
                    )
                    .clicked()
                    && target.part != part
                {
                    self.open_session(
                        ui.ctx(),
                        Target {
                            path: target.path.clone(),
                            part,
                            at_s: None,
                        },
                    );
                }
            }
        });
        ui.add_space(8.0);
        let text = match &state.viewer.text {
            None => {
                live(&ui.label(RichText::new("Reading…").color(p.muted)));
                return;
            }
            Some(Err(why)) => {
                live(&ui.label(RichText::new(why).color(ui.visuals().error_fg_color)));
                return;
            }
            Some(Ok(text)) => text.clone(),
        };
        match target.part {
            Part::Summary => {
                ui.add(
                    egui::Label::new(
                        RichText::new(
                            "Written by a language model on this machine. It can get details \
                             wrong; the time stamps open the transcript where each point comes from.",
                        )
                        .size(12.5)
                        .color(p.muted),
                    )
                    .wrap(),
                );
                ui.add_space(6.0);
                for line in text.lines() {
                    if let Some(title) = line.strip_prefix("# ") {
                        heading(
                            &ui.label(RichText::new(title).family(theme::bold()).size(16.0)),
                            title,
                            3,
                        );
                        continue;
                    }
                    if line.trim().is_empty() {
                        ui.add_space(6.0);
                        continue;
                    }
                    ui.horizontal_wrapped(|ui| {
                        ui.spacing_mut().item_spacing.x = 0.0;
                        for piece in sessions::pieces(line) {
                            match piece {
                                Piece::Text(text) => {
                                    ui.label(text.trim_matches('_'));
                                }
                                Piece::Stamp(seconds) => {
                                    let stamp = sessions::stamp(seconds);
                                    if ui.link(format!("[{stamp}]")).clicked() {
                                        self.open_session(
                                            ui.ctx(),
                                            Target {
                                                path: target.path.clone(),
                                                part: Part::Transcript,
                                                at_s: Some(seconds),
                                            },
                                        );
                                    }
                                }
                            }
                        }
                    });
                }
            }
            Part::Transcript => {
                let paragraphs = sessions::paragraphs(&text);
                let wanted = target
                    .at_s
                    .and_then(|at| sessions::paragraph_at(&paragraphs, at));
                for (index, (_, paragraph)) in paragraphs.iter().enumerate() {
                    let marked = Some(index) == wanted;
                    let shown = egui::Frame::NONE
                        .fill(if marked {
                            theme::selected_fill(p)
                        } else {
                            Color32::TRANSPARENT
                        })
                        .corner_radius(4.0)
                        .inner_margin(egui::Margin::symmetric(6, 4))
                        .show(ui, |ui| {
                            ui.add(egui::Label::new(paragraph.as_str()).wrap());
                        });
                    if marked && !state.viewer.scrolled {
                        shown.response.scroll_to_me(Some(Align::TOP));
                        self.sessions.lock().unwrap().viewer.scrolled = true;
                    }
                }
            }
        }
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
    response.widget_info(|| egui::WidgetInfo::selected(Role::Tab, true, selected, &name));
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
        if self.page == Page::Shortcuts && (regained || self.shown != Some(Page::Shortcuts)) {
            self.ask_shortcuts(ctx, Duration::ZERO);
        }
        if self.shown != Some(Page::Cleanup) && self.page == Page::Cleanup {
            self.refresh_model(ctx);
        }
        if self.page == Page::Sessions && (regained || self.shown != Some(Page::Sessions)) {
            self.refresh_sessions(ctx);
        }
        if self.page == Page::Sessions {
            self.follow_summaries(ctx);
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
        let wide = ctx.content_rect().width() >= SIDEBAR_FROM;
        let mut saved_settings = false;
        let mut saved_speech = false;

        egui::Panel::top("header")
            .frame(
                egui::Frame::NONE
                    .fill(header.bg)
                    .inner_margin(egui::Margin::symmetric(16, 10)),
            )
            .show(ui, |ui| {
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
            egui::Panel::left("pages")
                .resizable(false)
                .exact_size(SIDEBAR_WIDTH)
                .frame(
                    egui::Frame::NONE
                        .fill(p.bg)
                        .inner_margin(egui::Margin::same(8)),
                )
                .show(ui, |ui| {
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
            .show(ui, |ui| {
                // A solid bar, always drawn: the floating one only shows on hover, so a page
                // that runs past the window gave no sign there was more below.
                ui.spacing_mut().scroll = egui::style::ScrollStyle::solid();
                let scrolled = egui::ScrollArea::vertical()
                    .auto_shrink([false, false])
                    .scroll_bar_visibility(egui::scroll_area::ScrollBarVisibility::AlwaysVisible)
                    // Drag-to-scroll is for touch, and its invisible page-sized area took a Tab stop.
                    .scroll_source(egui::scroll_area::ScrollSource {
                        drag: egui::scroll_area::DragScroll::Never,
                        ..Default::default()
                    })
                    .show(ui, |ui| {
                        ui.set_max_width((ui.available_width() - SCROLL_GAP).min(FORM_MAX_WIDTH));
                        match self.page {
                            Page::Shortcuts => self.shortcuts(ui),
                            Page::Captions => saved_settings = self.captions(ui),
                            Page::Speech => saved_speech = self.speech(ui),
                            Page::Cleanup => self.cleanup(ui),
                            Page::Sessions => self.sessions(ui),
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

pub fn run(app_id: &str, page: Option<&str>, open: Option<&str>) -> ExitCode {
    let open = open.map(Target::parse);
    let page = if open.is_some() {
        Some(Page::Sessions)
    } else {
        page.and_then(Page::parse)
    };
    let options = eframe::NativeOptions {
        viewport: egui::ViewportBuilder::default()
            .with_title(TITLE)
            .with_app_id(app_id)
            .with_inner_size([820.0, 740.0])
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
        Box::new(|cc| {
            theme::install(&cc.egui_ctx);
            let mut window = SettingsWindow::new(page);
            if let Some(target) = open {
                window.open_session(&cc.egui_ctx, target);
            }
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
                    theme::apply_for_tests(ctx);
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
    fn the_cleanup_page_names_its_fields_and_asks_for_no_key() {
        let mut harness = harness(Page::Cleanup, WIDE);
        harness.state_mut().window.cleanup.enabled = true;
        harness.run();
        harness.get_by_role_and_label(Role::CheckBox, "Clean up dictation");
        harness.get_by_role_and_label(Role::ComboBox, "Run:");
        harness.get_by_role_and_label(Role::SpinButton, "Time limit:");
        for gone in ["Address:", "Model:"] {
            assert!(
                harness
                    .query_by_role_and_label(Role::TextInput, gone)
                    .is_none(),
                "{gone}"
            );
        }
        assert!(
            harness
                .query_by_role_and_label(Role::PasswordInput, "New key:")
                .is_none()
        );
        harness.get_by_label("Not downloaded (1.7 GB)");
        harness.get_by_label("Download the model below first; until then the raw text is typed.");
        let delete = harness.get_by_role_and_label(Role::Button, "Delete…");
        assert!(delete.accesskit_node().is_disabled(), "nothing to delete");
        harness.get_by_role_and_label(Role::Button, "Download");
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
        let limit = harness.get_by_role_and_label(Role::SpinButton, "Time limit:");
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

    fn listing() -> Listing {
        sessions::parse_listing(
            r#"{"directory": "/t", "auto": true, "model_ready": false, "backfill": 3,
               "sessions": [
                 {"path": "/t/b.vwsession", "name": "b.vwsession", "started": "2026-10-27T14:05:12-04:00",
                  "duration_s": 2530, "source": "system audio", "summary": true, "title": "Local captions talk",
                  "verified": true},
                 {"path": "/t/a.vwsession", "name": "a.vwsession", "started": "2026-10-26T09:00:00-04:00",
                  "duration_s": 40, "source": "microphone", "summary": false, "verified": true}]}"#,
        )
        .unwrap()
    }

    #[test]
    fn the_sessions_page_lists_each_session_with_what_it_can_do() {
        let mut harness = harness(Page::Sessions, WIDE);
        harness.state_mut().window.sessions.lock().unwrap().listing = Some(Ok(listing()));
        harness.run();
        harness.get_by_role_and_label(Role::CheckBox, "Summarise saved sessions automatically");
        harness.get_by_label("The model isn't downloaded yet; get it on the Cleanup page.");
        harness.get_by_role_and_label(Role::Button, "Bundle and summarise 3 transcripts");
        harness.get_by_label("2026-10-27 14:05 · 42 min · system audio");
        harness.get_by_label("Local captions talk");
        harness.get_by_label("Waiting for a summary");
        assert_eq!(
            harness
                .query_all_by_role_and_label(Role::Button, "Summary")
                .count(),
            2
        );
        harness.get_by_role_and_label(Role::Button, "Summarise again");
        harness.get_by_role_and_label(Role::Button, "Summarise");
        harness.get_by_role_and_label(Role::Button, "Delete summary");
        harness.get_by_label(
            "Saving transcripts is off (Captions page), so new sessions aren't kept.",
        );
    }

    #[test]
    fn a_summary_opens_with_its_stamps_as_links_and_says_a_model_wrote_it() {
        let mut harness = harness(Page::Sessions, WIDE);
        {
            let window = &mut harness.state_mut().window;
            let mut state = window.sessions.lock().unwrap();
            state.listing = Some(Ok(listing()));
            state.viewer = Viewer {
                target: Some(Target::parse("/t/b.vwsession#summary")),
                text: Some(Ok(
                    "# Local captions talk\n\nAn overview.\n\n- Runs locally [03:10]\n".into(),
                )),
                scrolled: false,
            };
        }
        harness.run();
        harness.get_by_role_and_label(Role::Button, "All sessions");
        harness.get_by_label("Local captions talk");
        harness.get_by_role_and_label(Role::Link, "[03:10]");
        harness.get_by_label(
            "Written by a language model on this machine. It can get details wrong; the time stamps open the transcript where each point comes from.",
        );
    }

    #[test]
    fn a_transcript_opened_at_a_stamp_marks_that_paragraph() {
        let mut harness = harness(Page::Sessions, WIDE);
        {
            let window = &mut harness.state_mut().window;
            let mut state = window.sessions.lock().unwrap();
            state.viewer = Viewer {
                target: Some(Target::parse("/t/b.vwsession#transcript@01:30")),
                text: Some(Ok(
                    "vinoWhisper transcript, x\n\n[00:00] One.\n\n[01:10] Two.\n".into(),
                )),
                scrolled: false,
            };
        }
        harness.run();
        harness.get_by_label("[01:10] Two.");
        assert!(
            harness
                .state()
                .window
                .sessions
                .lock()
                .unwrap()
                .viewer
                .scrolled
        );
    }

    #[test]
    fn a_session_being_summarised_shows_a_spinner_and_cannot_be_asked_again() {
        let mut harness = harness(Page::Sessions, WIDE);
        let mut working = listing();
        working.current = Some("/t/a.vwsession".into());
        working.queued = vec!["/t/b.vwsession".into()];
        harness.state_mut().window.sessions.lock().unwrap().listing = Some(Ok(working));
        harness.run_steps(2);
        harness.get_by_label("Summarising…");
        harness.get_by_label("Queued for a summary");
        for name in ["Summarise", "Summarise again"] {
            let button = harness.get_by_role_and_label(Role::Button, name);
            assert!(button.accesskit_node().is_disabled(), "{name}");
        }
    }
}
