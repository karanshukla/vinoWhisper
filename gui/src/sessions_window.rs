use std::path::PathBuf;
use std::process::ExitCode;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use eframe::egui::{self, Align, Color32, Layout, RichText};
use vino_ui::a11y;
use vino_ui::icons::Icon;
use vino_ui::theme;
use vino_ui::widgets::{self, Button, ButtonKind, ChipKind, Tone};

use crate::forward;
use crate::sessions::{
    self, ExportRequest, Exported, Format, ImportEvent, Listing, OpenIn, Opened, Part, Piece,
    Scope, SummaryLine, Target,
};
use crate::speak::{self, Reading};
use vinowhisper_settings::Settings;

mod export;
mod import;
mod list;
mod open_in;
mod parts;
mod reading;
mod viewer;

use parts::*;

#[derive(Clone, Default)]
struct Viewer {
    target: Option<Target>,
    text: Option<Result<String, String>>,
    jumped_to_stamp: bool,
    copied: bool,
}

#[derive(Clone, Default)]
struct SessionsState {
    listing: Option<Result<Listing, String>>,
    listed_at: Option<std::time::Instant>,
    refreshing: bool,
    viewer: Viewer,
    busy: bool,
    error: String,
    exporting: bool,
    exported: Option<Result<Exported, String>>,
    opening: bool,
    opened: Option<Result<Opened, String>>,
    import: Option<Import>,
    imported: Option<Result<PathBuf, String>>,
    import_warnings: Vec<String>,
    relist: bool,
}

// `name` is empty until the dialog answers.
#[derive(Clone, Default)]
struct Import {
    name: String,
    position_s: f64,
    duration_s: Option<f64>,
    pid: Option<u32>,
    cancelled: bool,
    warnings: Vec<String>,
}

#[derive(Clone)]
struct ExportDialog {
    path: PathBuf,
    scope: Scope,
    format: Format,
    has_summary: bool,
}

// Tests swap these for stand-ins; `Ok(None)` is a dialog the user closed.
type Exporter = Arc<dyn Fn(&ExportRequest) -> Result<Option<Exported>, String> + Send + Sync>;
type Opener = Arc<dyn Fn(&ExportRequest) -> Result<Option<Opened>, String> + Send + Sync>;
type Importer =
    Arc<dyn Fn(&mut dyn FnMut(ImportEvent)) -> Result<Option<PathBuf>, String> + Send + Sync>;

const REFRESH_EVERY: Duration = Duration::from_secs(2);

// (pinned by `tests::a_narrow_window_keeps_import_as_a_named_plus_button`)
const NARROW_BELOW: f32 = 560.0;
const LIST_MAX_WIDTH: f32 = 760.0;
const READING_MAX_WIDTH: f32 = 640.0;

struct SessionsWindow {
    state: Arc<Mutex<SessionsState>>,
    confirm_delete: Option<PathBuf>,
    save_transcripts: bool,
    focused: bool,
    listed_once: bool,
    kde_header: Option<theme::KdeHeader>,
    app_title_bar: bool,
    text_scale: Option<f32>,
    export_dialog: Option<ExportDialog>,
    exporter: Exporter,
    // (pinned by `tests::read_aloud_is_not_offered_without_vinospeak`)
    speaker: Option<PathBuf>,
    reading: Option<Reading>,
    read_error: Option<String>,
    open_in: OpenIn,
    settings_path: PathBuf,
    opener: Opener,
    importer: Importer,
}

impl SessionsWindow {
    fn with(save_transcripts: bool) -> Self {
        SessionsWindow {
            state: Arc::new(Mutex::new(SessionsState::default())),
            confirm_delete: None,
            save_transcripts,
            focused: true,
            listed_once: false,
            kde_header: None,
            app_title_bar: false,
            text_scale: None,
            export_dialog: None,
            exporter: Arc::new(sessions::export_with_dialog),
            speaker: None,
            reading: None,
            read_error: None,
            open_in: OpenIn::default(),
            settings_path: Settings::path(),
            opener: Arc::new(sessions::open_in),
            importer: Arc::new(sessions::import_with_dialog),
        }
    }

    fn follow_text_scale(&mut self, ctx: &egui::Context, scale: Option<f32>) {
        if scale != self.text_scale {
            self.text_scale = scale;
            ctx.set_zoom_factor(scale.unwrap_or(1.0));
        }
    }

    fn refresh(&self, ctx: &egui::Context) {
        let state = Arc::clone(&self.state);
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
        let state = self.state.lock().unwrap();
        let working = matches!(&state.listing, Some(Ok(listing)) if listing.working());
        if !working {
            return;
        }
        let due = state
            .listed_at
            .is_none_or(|at| at.elapsed() >= REFRESH_EVERY);
        drop(state);
        if due {
            self.refresh(ctx);
        }
        ctx.request_repaint_after(REFRESH_EVERY);
    }

    fn open(&self, ctx: &egui::Context, target: Target) {
        let state = Arc::clone(&self.state);
        let ctx = ctx.clone();
        state.lock().unwrap().viewer = Viewer {
            target: Some(target.clone()),
            ..Viewer::default()
        };
        std::thread::spawn(move || {
            let text = sessions::show(&target.path, target.part);
            let mut state = state.lock().unwrap();
            // A quicker second click has replaced it.
            if state.viewer.target.as_ref() == Some(&target) {
                state.viewer.text = Some(text);
            }
            drop(state);
            ctx.request_repaint();
        });
    }

    fn act(&self, ctx: &egui::Context, action: &'static str, path: Option<PathBuf>) {
        let state = Arc::clone(&self.state);
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

    // Draws without listing, reading or spawning, so tests can drive it.
    fn show(&mut self, ui: &mut egui::Ui) {
        let ctx = &ui.ctx().clone();
        ctx.accesskit_node_builder(egui::accesskit_root_id(), |node| node.set_label(TITLE));
        let p = theme::palette(ctx);
        let header = theme::header(ctx, self.kde_header.as_ref(), self.focused);
        self.follow_reading(ctx);
        let state = self.state.lock().unwrap().clone();
        let viewing = state.viewer.target.is_some();

        egui::Panel::top("header")
            .show_separator_line(false)
            .frame(widgets::header_frame(header, p))
            .show(ui, |ui| {
                if self.app_title_bar {
                    vino_ui::window::drag(ui, ui.clip_rect(), "header");
                    // Not in a row: the header's first row differs between list and session.
                    vino_ui::window::corner(ui);
                    // A press on a label drags the window.
                    ui.style_mut().interaction.selectable_labels = false;
                }
                if viewing {
                    self.viewer_header(ui, &state, header);
                } else {
                    ui.horizontal(|ui| {
                        ui.spacing_mut().item_spacing.x = 10.0;
                        vino_ui::logo::whisper(ui, 24.0);
                        widgets::window_title(ui, "Saved sessions", header.text);
                        ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                            if self.app_title_bar {
                                ui.add_space(controls_room(ui));
                            }
                            if ui.add(Button::ghost("Settings")).clicked() {
                                forward::open_settings("sessions");
                            }
                            let name = "Import audio or video…";
                            let idle = state.import.is_none();
                            let import = if ctx.content_rect().width() < NARROW_BELOW {
                                ui.add_enabled_ui(idle, |ui| {
                                    widgets::icon_button(
                                        ui,
                                        ButtonKind::Secondary,
                                        Icon::Plus,
                                        name,
                                    )
                                })
                                .inner
                            } else {
                                ui.add_enabled(idle, Button::secondary(name))
                            };
                            if import.clicked() {
                                self.import(ctx);
                            }
                        });
                    });
                }
            });

        egui::CentralPanel::default()
            .frame(
                egui::Frame::NONE
                    .fill(p.bg)
                    // No right margin: the scroll bar belongs on the window's edge.
                    .inner_margin(egui::Margin {
                        left: 20,
                        right: 0,
                        top: 4,
                        bottom: 0,
                    }),
            )
            .show(ui, |ui| {
                let (what, width) = if viewing {
                    ("the session", READING_MAX_WIDTH)
                } else {
                    ("saved sessions", LIST_MAX_WIDTH)
                };
                widgets::page_scroll(ui, what, width, |ui| {
                    self.read_notice(ui);
                    if viewing {
                        self.viewer(ui, &state);
                    } else {
                        self.list(ui, &state);
                    }
                });
            });
    }
}

impl eframe::App for SessionsWindow {
    fn raw_input_hook(&mut self, _ctx: &egui::Context, raw: &mut egui::RawInput) {
        vino_ui::input::faster_touchpad(raw);
    }

    fn ui(&mut self, ui: &mut egui::Ui, _frame: &mut eframe::Frame) {
        let ctx = &ui.ctx().clone();
        let focused = ctx.input(|input| input.focused);
        let regained = focused && !self.focused;
        if regained {
            theme::follow_desktop(ctx);
            self.kde_header = theme::kde_header();
            self.follow_text_scale(ctx, theme::desktop_text_scale());
            self.save_transcripts = Settings::load().save_transcripts;
        }
        self.focused = focused;
        let relist = std::mem::take(&mut self.state.lock().unwrap().relist);
        if regained || relist || !self.listed_once {
            self.listed_once = true;
            self.refresh(ctx);
        }
        self.follow_summaries(ctx);
        self.show(ui);
        if self.app_title_bar {
            vino_ui::window::edges(ui);
        }
    }
}

const TITLE: &str = "vinoWhisper sessions";

pub fn run(app_id: &str, open: Option<&str>) -> ExitCode {
    let open = open.map(Target::parse);
    let app_title_bar = vino_ui::window::app_title_bar();
    let options = eframe::NativeOptions {
        viewport: vino_ui::window::viewport(
            egui::ViewportBuilder::default()
                .with_title(TITLE)
                .with_app_id(app_id)
                .with_inner_size([820.0, 680.0])
                .with_min_inner_size([340.0, 320.0]),
            app_title_bar,
        ),
        // See window::run: vsync stalls a window KWin considers hidden.
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
            let settings = Settings::load();
            let mut window = SessionsWindow::with(settings.save_transcripts);
            window.speaker = speak::find_on_path("vinospeak");
            window.open_in = settings.open_in;
            window.kde_header = theme::kde_header();
            window.app_title_bar = app_title_bar;
            if let Some(target) = open {
                window.open(&cc.egui_ctx, target);
            }
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
mod tests;
