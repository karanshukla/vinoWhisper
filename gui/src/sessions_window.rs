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
    self, ExportRequest, Exported, Format, Listing, Part, Piece, Scope, SummaryLine, Target,
};
use crate::speak::{self, Reading};
use vinowhisper_settings::Settings;

#[derive(Clone, Default)]
struct Viewer {
    target: Option<Target>,
    text: Option<Result<String, String>>,
    // Scrolled to target.at_s already; only the first frame after loading jumps.
    scrolled: bool,
    copied: bool,
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
    exporting: bool,
    exported: Option<Result<Exported, String>>,
}

#[derive(Clone)]
struct ExportDialog {
    path: PathBuf,
    scope: Scope,
    format: Format,
    has_summary: bool,
}

// Asks where (the save dialog) and writes; Ok(None) when the user cancelled. Tests swap it.
type Exporter = Arc<dyn Fn(&ExportRequest) -> Result<Option<Exported>, String> + Send + Sync>;

const REFRESH_EVERY: Duration = Duration::from_secs(2);

const LIST_MAX_WIDTH: f32 = 760.0;
// Narrower than the list: this is for reading.
const READING_MAX_WIDTH: f32 = 640.0;

struct SessionsWindow {
    state: Arc<Mutex<SessionsState>>,
    confirm_delete: Option<PathBuf>,
    save_transcripts: bool,
    focused: bool,
    listed_once: bool,
    kde_header: Option<theme::KdeHeader>,
    text_scale: Option<f32>,
    export_dialog: Option<ExportDialog>,
    exporter: Exporter,
    // vinoSpeak, looked up once when the window opens; no button without it.
    speaker: Option<PathBuf>,
    reading: Option<Reading>,
    read_error: Option<String>,
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
            text_scale: None,
            export_dialog: None,
            exporter: Arc::new(sessions::export_with_dialog),
            speaker: None,
            reading: None,
            read_error: None,
        }
    }

    fn reading(&self, path: &std::path::Path) -> bool {
        self.reading.as_ref().is_some_and(|r| r.path == path)
    }

    // One reading at a time: starting one, or Stop, ends the last.
    fn read_aloud(&mut self, path: &std::path::Path, part: Part) {
        let was = self.reading.take().map(|r| r.path.clone());
        self.read_error = None;
        if was.as_deref() == Some(path) {
            return;
        }
        let Some(program) = &self.speaker else {
            return;
        };
        match Reading::start(program, path, part) {
            Ok(reading) => self.reading = Some(reading),
            Err(why) => self.read_error = Some(why),
        }
    }

    fn stop_reading_unless(&mut self, path: Option<&std::path::Path>) {
        if self
            .reading
            .as_ref()
            .is_some_and(|r| Some(r.path.as_path()) != path)
        {
            self.reading = None;
        }
    }

    fn follow_reading(&mut self, ctx: &egui::Context) {
        let Some(reading) = self.reading.as_mut() else {
            return;
        };
        match reading.poll() {
            None => ctx.request_repaint_after(Duration::from_millis(200)),
            Some(result) => {
                self.reading = None;
                self.read_error = result.err();
            }
        }
    }

    fn read_notice(&mut self, ui: &mut egui::Ui) {
        if let Some(why) = self.read_error.clone() {
            ui.add_space(12.0);
            if widgets::notice(ui, Tone::Error, &format!("Reading aloud stopped: {why}")) {
                self.read_error = None;
            }
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
            // Only if it is still the one asked for: a quick second click wins.
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

    fn ask_export(&mut self, path: PathBuf, scope: Scope, has_summary: bool) {
        self.export_dialog = Some(ExportDialog {
            path,
            scope: if has_summary {
                scope
            } else {
                Scope::Transcript
            },
            format: Format::Markdown,
            has_summary,
        });
    }

    fn export(&self, ctx: &egui::Context, request: ExportRequest) {
        let state = Arc::clone(&self.state);
        let exporter = Arc::clone(&self.exporter);
        let ctx = ctx.clone();
        {
            let mut state = state.lock().unwrap();
            state.exporting = true;
            state.exported = None;
        }
        std::thread::spawn(move || {
            let result = exporter(&request);
            let mut state = state.lock().unwrap();
            state.exporting = false;
            state.exported = result.transpose();
            drop(state);
            ctx.request_repaint();
        });
    }

    // In place, like a confirmation, rather than a modal over a window that may be half a screen.
    fn export_panel(&mut self, ui: &mut egui::Ui, path: &std::path::Path) {
        let Some(dialog) = self.export_dialog.as_mut().filter(|d| d.path == path) else {
            return;
        };
        let mut save = false;
        let mut cancel = false;
        widgets::group_label(ui, "Export");
        ui.add_space(6.0);
        widgets::group(ui, |ui| {
            if dialog.has_summary {
                widgets::Row::new("Part").show(ui, |ui, _| {
                    widgets::segmented(
                        ui,
                        "Part",
                        &mut dialog.scope,
                        &[
                            (Scope::Summary, "Summary"),
                            (Scope::Transcript, "Transcript"),
                            (Scope::Both, "Both"),
                        ],
                    );
                });
            } else {
                widgets::Row::new("Part")
                    .help("The transcript: this session has no summary yet.")
                    .show(ui, |_, _| {});
            }
            let help = match dialog.format {
                Format::Markdown => {
                    "For notes apps like Obsidian. The session's details go in its frontmatter."
                }
                Format::Text => "No markup. Opens anywhere.",
                Format::Odt => "For LibreOffice, Word and Google Docs.",
            };
            widgets::Row::new("Format").help(help).show(ui, |ui, _| {
                widgets::segmented(
                    ui,
                    "Format",
                    &mut dialog.format,
                    &[
                        (Format::Markdown, "Markdown"),
                        (Format::Text, "Plain text"),
                        (Format::Odt, "Document (.odt)"),
                    ],
                );
            });
            widgets::Row::new("Where")
                .help("The desktop's save dialog asks next.")
                .show(ui, |ui, _| {
                    save = ui.add(Button::primary("Save…").small()).clicked();
                    cancel = ui.add(Button::ghost("Cancel").small()).clicked();
                });
        });
        if save {
            let request = ExportRequest {
                path: dialog.path.clone(),
                scope: dialog.scope,
                format: dialog.format,
            };
            self.export_dialog = None;
            self.export(ui.ctx(), request);
        } else if cancel {
            self.export_dialog = None;
        }
    }

    // Where an export went, or why it failed, until dismissed.
    fn export_notice(&self, ui: &mut egui::Ui, state: &SessionsState) {
        if state.exporting {
            ui.add_space(12.0);
            a11y::live(&widgets::small(ui, "Waiting for the save dialog."));
            return;
        }
        let Some(result) = &state.exported else {
            return;
        };
        ui.add_space(12.0);
        let dismissed = match result {
            Ok(exported) => {
                let mut text = if exported.fallback {
                    format!(
                        "There's no save dialog on this desktop, so it's in {}.",
                        exported.path.display()
                    )
                } else {
                    format!("Exported to {}.", exported.path.display())
                };
                if exported.summary_missing {
                    text.push_str(" The transcript only: there's no summary yet.");
                }
                widgets::banner(ui, Tone::Info, &text, |ui| {
                    let dismissed = ui.add(Button::ghost("Dismiss").small()).clicked();
                    if ui
                        .add(Button::secondary("Show in folder").small())
                        .clicked()
                    {
                        sessions::show_in_folder(&exported.path);
                    }
                    dismissed
                })
            }
            Err(why) => widgets::notice(ui, Tone::Error, &format!("Export didn't work: {why}")),
        };
        if dismissed {
            self.state.lock().unwrap().exported = None;
        }
    }

    fn list(&mut self, ui: &mut egui::Ui, state: &SessionsState) {
        let p = theme::palette(ui.ctx());
        let listing = match &state.listing {
            Some(Ok(listing)) => Some(listing.clone()),
            _ => None,
        };
        self.export_notice(ui, state);
        if listing.as_ref().is_some_and(|l| !l.model_ready) {
            ui.add_space(12.0);
            widgets::banner(
                ui,
                Tone::Warn,
                "The summary model isn't downloaded yet; get it on the Cleanup page of the settings.",
                |ui| {
                    if ui.add(Button::secondary("Open settings").small()).clicked() {
                        forward::open_settings("cleanup");
                    }
                },
            );
        }
        if !self.save_transcripts {
            ui.add_space(12.0);
            widgets::banner(
                ui,
                Tone::Info,
                "Saving transcripts is off (settings, Captions page), so new sessions aren't kept.",
                |_| {},
            );
        }
        if let Some(listing) = listing.as_ref().filter(|l| l.backfill > 0) {
            let count = listing.backfill;
            ui.add_space(12.0);
            let what = format!("{count} transcript{}", if count == 1 { "" } else { "s" });
            egui::Frame::new()
                .fill(p.field)
                .corner_radius(12.0)
                .inner_margin(egui::Margin::symmetric(14, 12))
                .show(ui, |ui| {
                    ui.set_width(ui.available_width());
                    ui.horizontal(|ui| {
                        ui.spacing_mut().item_spacing.x = 12.0;
                        ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                            let button = ui.add_enabled(
                                !state.busy,
                                Button::secondary("Bundle and summarise"),
                            );
                            if a11y::for_what(button, &what).clicked() {
                                self.act(ui.ctx(), "backfill", None);
                            }
                            ui.with_layout(Layout::top_down(Align::Min), |ui| {
                                ui.spacing_mut().item_spacing.y = 2.0;
                                ui.label(
                                    RichText::new(format!("{what} from before summaries"))
                                        .family(theme::semibold()),
                                );
                                widgets::small(ui, "Plain text. Bundling keeps the text as it is.");
                            });
                        });
                    });
                });
        }

        let n = listing.as_ref().map_or(0, |l| l.sessions.len());
        section(ui, "Sessions", n);
        if !state.error.is_empty() {
            widgets::notice(
                ui,
                Tone::Error,
                &format!("That didn't work: {}", state.error),
            );
            ui.add_space(8.0);
        }
        match &state.listing {
            None => {
                a11y::live(&widgets::chip_busy(ui, ChipKind::Neutral, "Reading…"));
            }
            Some(Err(why)) => {
                widgets::banner(ui, Tone::Error, why, |_| {});
            }
            Some(Ok(listing)) if listing.sessions.is_empty() => {
                widgets::muted(ui, "No saved sessions yet.");
            }
            Some(Ok(listing)) => {
                for entry in &listing.sessions {
                    self.card(ui, entry, listing, state.busy);
                    self.export_panel(ui, &entry.path);
                    ui.add_space(8.0);
                }
            }
        }
    }

    fn card(&mut self, ui: &mut egui::Ui, entry: &sessions::Entry, listing: &Listing, busy: bool) {
        let p = theme::palette(ui.ctx());
        let path = entry.path.clone();
        let deleting = self.confirm_delete.as_ref() == Some(&path);
        let (fill, stroke) = if deleting {
            (p.error_tint, egui::Stroke::NONE)
        } else {
            (p.surface, egui::Stroke::new(1.0_f32, p.rule))
        };
        egui::Frame::new()
            .fill(fill)
            .stroke(stroke)
            .corner_radius(12.0)
            .inner_margin(egui::Margin::symmetric(14, 12))
            .show(ui, |ui| {
                ui.set_width(ui.available_width());
                let working = listing.current.as_ref() == Some(&entry.path);
                let waiting = working || listing.queued.contains(&entry.path);
                // The title leads once there is one; until then the date stands in for it.
                let title = entry.title.clone().filter(|_| entry.error.is_none());
                let name = title.clone().unwrap_or_else(|| entry.when());
                let mut facts = Vec::new();
                if title.is_some() {
                    facts.push(entry.when());
                }
                facts.extend(entry.length());
                facts.extend(entry.source.clone());
                ui.horizontal(|ui| {
                    ui.spacing_mut().item_spacing.x = 8.0;
                    ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                        if deleting {
                            if ui
                                .add(Button::destructive("Delete the session").small())
                                .clicked()
                            {
                                self.confirm_delete = None;
                                self.act(ui.ctx(), "delete", Some(path.clone()));
                            }
                            if ui.add(Button::ghost("Cancel").small()).clicked() {
                                self.confirm_delete = None;
                            }
                        } else {
                            self.card_actions(ui, entry, listing, busy, waiting, working);
                        }
                        ui.with_layout(Layout::top_down(Align::Min), |ui| {
                            ui.spacing_mut().item_spacing.y = 2.0;
                            let shown = ui.add(
                                egui::Label::new(RichText::new(&name).family(theme::semibold()))
                                    .truncate(),
                            );
                            a11y::heading(&shown, &name, 3);
                            if deleting {
                                ui.label(
                                    RichText::new("Delete this session, transcript and summary?")
                                        .size(12.5)
                                        .color(p.error),
                                );
                            } else {
                                widgets::small(ui, facts.join(" · "));
                                if let Some(error) = &entry.error {
                                    a11y::live(
                                        &ui.add(
                                            egui::Label::new(
                                                RichText::new(error).size(12.5).color(p.error),
                                            )
                                            .wrap(),
                                        ),
                                    );
                                }
                                if !entry.verified && entry.error.is_none() {
                                    ui.label(
                                        RichText::new("Changed since it was written.")
                                            .size(12.5)
                                            .color(p.warn),
                                    );
                                }
                            }
                        });
                    });
                });
            });
    }

    // Right to left: the More menu, Summary or Summarise, Transcript, then the status chip.
    fn card_actions(
        &mut self,
        ui: &mut egui::Ui,
        entry: &sessions::Entry,
        listing: &Listing,
        busy: bool,
        waiting: bool,
        working: bool,
    ) {
        let path = entry.path.clone();
        let readable = entry.error.is_none();
        let open = |part| Target {
            path: path.clone(),
            part,
            at_s: None,
        };
        let more = widgets::icon_button(ui, ButtonKind::Secondary, Icon::More, "More");
        egui::Popup::menu(&more).show(|ui| {
            ui.set_min_width(200.0);
            if entry.summary
                && ui
                    .add_enabled(
                        readable && !busy && !waiting,
                        egui::Button::new("Summarise again"),
                    )
                    .clicked()
            {
                self.act(ui.ctx(), "summarise", Some(path.clone()));
                ui.close();
            }
            if entry.summary
                && ui
                    .add_enabled(!busy, egui::Button::new("Delete summary"))
                    .clicked()
            {
                self.act(ui.ctx(), "delete-summary", Some(path.clone()));
                ui.close();
            }
            if ui.button("Show in folder").clicked() {
                sessions::show_in_folder(&path);
                ui.close();
            }
            if ui
                .add_enabled(readable, egui::Button::new("Export…"))
                .clicked()
            {
                self.ask_export(path.clone(), Scope::Both, entry.summary);
                ui.close();
            }
            if self.speaker.is_some() {
                let label = if self.reading(&path) {
                    "Stop reading"
                } else {
                    "Read aloud"
                };
                if ui.add_enabled(readable, egui::Button::new(label)).clicked() {
                    let part = if entry.summary {
                        Part::Summary
                    } else {
                        Part::Transcript
                    };
                    self.read_aloud(&path, part);
                    ui.close();
                }
            }
            ui.separator();
            let p = theme::palette(ui.ctx());
            if ui
                .add_enabled(
                    !busy,
                    egui::Button::new(RichText::new("Delete…").color(p.error)),
                )
                .clicked()
            {
                self.confirm_delete = Some(path.clone());
                ui.close();
            }
        });
        if entry.summary {
            if ui
                .add_enabled(readable, Button::primary("Summary").small())
                .clicked()
            {
                self.stop_reading_unless(Some(&path));
                self.open(ui.ctx(), open(Part::Summary));
            }
        } else if ui
            .add_enabled(
                readable && !busy && !waiting,
                Button::secondary("Summarise").small(),
            )
            .clicked()
        {
            self.act(ui.ctx(), "summarise", Some(path.clone()));
        }
        if ui
            .add_enabled(readable, Button::secondary("Transcript").small())
            .clicked()
        {
            self.stop_reading_unless(Some(&path));
            self.open(ui.ctx(), open(Part::Transcript));
        }
        if entry.error.is_none() && (waiting || !entry.summary) {
            let text = listing.status(entry);
            let shown = if working {
                widgets::chip_busy(ui, ChipKind::Ok, text)
            } else {
                widgets::chip(ui, ChipKind::Neutral, text)
            };
            a11y::live(&shown);
        }
    }

    // What the viewer puts in the header: the way back, the title and facts, Show in folder
    // and the two tabs.
    fn viewer_header(&mut self, ui: &mut egui::Ui, state: &SessionsState, header: theme::Header) {
        let p = theme::palette(ui.ctx());
        let Some(target) = state.viewer.target.clone() else {
            return;
        };
        if ui
            .add(Button::ghost("All sessions").icon(Icon::ArrowLeft))
            .clicked()
        {
            self.state.lock().unwrap().viewer = Viewer::default();
            self.reading = None;
            self.refresh(ui.ctx());
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
        let title = entry.as_ref().and_then(|e| e.title.clone());
        let name = match (&title, &entry) {
            (Some(title), _) => title.clone(),
            (None, Some(entry)) => entry.when(),
            (None, None) => target
                .path
                .file_name()
                .map(|n| n.to_string_lossy().into_owned())
                .unwrap_or_default(),
        };
        ui.add_space(8.0);
        ui.horizontal(|ui| {
            ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                if ui.add(Button::secondary("Show in folder")).clicked() {
                    sessions::show_in_folder(&target.path);
                }
                let has_summary = entry.as_ref().is_none_or(|e| e.summary);
                if ui.add(Button::secondary("Export…")).clicked() {
                    self.ask_export(target.path.clone(), target.part.into(), has_summary);
                }
                ui.with_layout(Layout::top_down(Align::Min), |ui| {
                    ui.spacing_mut().item_spacing.y = 2.0;
                    let shown = ui.add(
                        egui::Label::new(
                            RichText::new(&name)
                                .size(22.0)
                                .family(theme::bold())
                                .color(header.text),
                        )
                        .truncate(),
                    );
                    a11y::heading(&shown, &name, 2);
                    if let Some(entry) = &entry {
                        let mut facts = Vec::new();
                        if title.is_some() {
                            facts.push(entry.when());
                        }
                        facts.extend(entry.length());
                        facts.extend(entry.source.clone());
                        ui.label(
                            RichText::new(facts.join(" · "))
                                .size(12.5)
                                .color(header.muted),
                        );
                    }
                });
            });
        });
        ui.add_space(6.0);
        ui.horizontal(|ui| {
            ui.spacing_mut().item_spacing.x = 18.0;
            a11y::tab_list(ui, "View");
            for (part, label) in [(Part::Transcript, "Transcript"), (Part::Summary, "Summary")] {
                let enabled = part == Part::Transcript || entry.as_ref().is_none_or(|e| e.summary);
                if widgets::tab(ui, label, target.part == part, enabled, header).clicked()
                    && target.part != part
                {
                    self.open(
                        ui.ctx(),
                        Target {
                            path: target.path.clone(),
                            part,
                            at_s: None,
                        },
                    );
                }
            }
            // Both act on the tab being read, so they sit with the tabs.
            let text = match &state.viewer.text {
                Some(Ok(text)) => Some(text.clone()),
                _ => None,
            };
            ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                let label = if state.viewer.copied {
                    "Copied"
                } else {
                    "Copy"
                };
                let copy = ui.add_enabled(text.is_some(), Button::ghost(label).small());
                if let Some(text) = &text
                    && copy.clicked()
                {
                    ui.ctx().copy_text(text.clone());
                    self.state.lock().unwrap().viewer.copied = true;
                }
                if self.speaker.is_some() {
                    let label = if self.reading(&target.path) {
                        "Stop"
                    } else {
                        "Read aloud"
                    };
                    let button = ui.add_enabled(
                        text.is_some() || self.reading(&target.path),
                        Button::secondary(label).small(),
                    );
                    if button.clicked() {
                        self.read_aloud(&target.path, target.part);
                    }
                }
            });
        });
        let _ = p;
    }

    fn viewer(&mut self, ui: &mut egui::Ui, state: &SessionsState) {
        let Some(target) = state.viewer.target.clone() else {
            return;
        };
        let entry = match &state.listing {
            Some(Ok(listing)) => listing
                .sessions
                .iter()
                .find(|e| e.path == target.path)
                .cloned(),
            _ => None,
        };
        let title = entry.as_ref().and_then(|e| e.title.clone());
        self.export_notice(ui, state);
        self.export_panel(ui, &target.path);
        ui.add_space(8.0);
        let text = match &state.viewer.text {
            None => {
                a11y::live(&widgets::chip_busy(ui, ChipKind::Neutral, "Reading…"));
                return;
            }
            Some(Err(why)) => {
                widgets::banner(ui, Tone::Error, why, |_| {});
                return;
            }
            Some(Ok(text)) => text.clone(),
        };
        match target.part {
            Part::Summary => self.summary(ui, &target, &text, title.as_deref()),
            Part::Transcript => self.transcript(ui, state, &target, &text),
        }
    }

    // A line with stamps in it, each a link into the transcript as a small tinted pill.
    // Returns the first piece drawn, for a heading to be marked on.
    fn stamped(
        &self,
        ui: &mut egui::Ui,
        target: &Target,
        line: &str,
        bold: bool,
    ) -> egui::Response {
        let styled = |text: &str| {
            let text = RichText::new(text).size(15.0);
            if bold {
                text.family(theme::semibold())
            } else {
                text
            }
        };
        let pieces = sessions::pieces(line);
        if !pieces.iter().any(|piece| matches!(piece, Piece::Stamp(_))) {
            return ui.add(egui::Label::new(styled(line)).wrap());
        }
        ui.horizontal_wrapped(|ui| {
            ui.spacing_mut().item_spacing.x = 0.0;
            let mut first: Option<egui::Response> = None;
            for piece in pieces {
                match piece {
                    Piece::Text(part) => {
                        let shown = ui.label(styled(&part));
                        first.get_or_insert(shown);
                    }
                    Piece::Stamp(seconds) => {
                        ui.add_space(4.0);
                        if stamp_pill(ui, seconds).clicked() {
                            self.open(
                                ui.ctx(),
                                Target {
                                    path: target.path.clone(),
                                    part: Part::Transcript,
                                    at_s: Some(seconds),
                                },
                            );
                        }
                        ui.add_space(4.0);
                    }
                }
            }
            first
        })
        .inner
        .unwrap_or_else(|| ui.response())
    }

    fn summary(&self, ui: &mut egui::Ui, target: &Target, text: &str, title: Option<&str>) {
        let p = theme::palette(ui.ctx());
        ui.spacing_mut().item_spacing.y = 6.0;
        let mut first_heading = true;
        for line in text.lines() {
            match sessions::summary_line(line) {
                SummaryLine::Blank => ui.add_space(4.0),
                // The title is already the viewer's heading.
                SummaryLine::Heading(heading_text)
                    if first_heading && Some(heading_text.as_str()) == title =>
                {
                    first_heading = false;
                }
                SummaryLine::Heading(heading_text) => {
                    first_heading = false;
                    ui.add_space(10.0);
                    let shown = self.stamped(ui, target, &heading_text, true);
                    a11y::heading(&shown, &heading_text, 3);
                }
                SummaryLine::Bullet(item) => {
                    ui.horizontal_top(|ui| {
                        ui.spacing_mut().item_spacing.x = 10.0;
                        ui.label(RichText::new("•").size(15.0).color(p.muted));
                        ui.vertical(|ui| {
                            self.stamped(ui, target, &item, false);
                        });
                    });
                }
                SummaryLine::Note(note) => {
                    ui.add_space(6.0);
                    widgets::small(ui, note);
                }
                SummaryLine::Text(line) => {
                    self.stamped(ui, target, &line, false);
                }
            }
        }
    }

    fn transcript(&self, ui: &mut egui::Ui, state: &SessionsState, target: &Target, text: &str) {
        let p = theme::palette(ui.ctx());
        ui.spacing_mut().item_spacing.y = 4.0;
        let paragraphs = sessions::paragraphs(text);
        let wanted = target
            .at_s
            .and_then(|at| sessions::paragraph_at(&paragraphs, at));
        for (index, (at, paragraph)) in paragraphs.iter().enumerate() {
            // The header line ("vinoWhisper transcript, <date>, <source>, <device>") is what the
            // heading above already says.
            if index == 0 && at.is_none() {
                continue;
            }
            let marked = Some(index) == wanted;
            let mut job = egui::text::LayoutJob::default();
            job.wrap.max_width = ui.available_width() - 20.0;
            let body = egui::TextFormat {
                font_id: egui::FontId::proportional(15.0),
                color: p.text,
                line_height: Some(24.0),
                ..Default::default()
            };
            let (stamp, words) = match at {
                Some(_) => paragraph
                    .split_once("] ")
                    .map_or(("", paragraph.as_str()), |(s, w)| (s, w)),
                None => ("", paragraph.as_str()),
            };
            if !stamp.is_empty() {
                job.append(
                    &format!("{stamp}] "),
                    0.0,
                    egui::TextFormat {
                        font_id: egui::FontId::monospace(12.5),
                        color: p.muted,
                        ..body.clone()
                    },
                );
                job.append(words, 0.0, body);
            } else {
                job.append(words, 0.0, body);
            }
            let shown = egui::Frame::NONE
                .fill(if marked { p.tint } else { Color32::TRANSPARENT })
                .corner_radius(8.0)
                .inner_margin(egui::Margin::symmetric(10, 6))
                // Out by the padding, so the text lines up with the heading above it.
                .outer_margin(egui::Margin {
                    left: -10,
                    right: 0,
                    top: 0,
                    bottom: 0,
                })
                .show(ui, |ui| {
                    ui.add(egui::Label::new(job).wrap());
                });
            if marked && !state.viewer.scrolled {
                shown.response.scroll_to_me(Some(Align::TOP));
                self.state.lock().unwrap().viewer.scrolled = true;
            }
        }
    }

    // The window minus its side effects but the buttons', for tests to drive alone.
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
                if viewing {
                    self.viewer_header(ui, &state, header);
                } else {
                    ui.horizontal(|ui| {
                        ui.spacing_mut().item_spacing.x = 10.0;
                        vino_ui::logo::whisper(ui, 24.0);
                        widgets::window_title(ui, "Saved sessions", header.text);
                        ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                            if ui.add(Button::ghost("Settings")).clicked() {
                                forward::open_settings("sessions");
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

// A time stamp as a small tinted pill, a link into the transcript.
fn stamp_pill(ui: &mut egui::Ui, seconds: u64) -> egui::Response {
    let p = theme::palette(ui.ctx());
    let text = format!("[{}]", sessions::stamp(seconds));
    let galley = ui.painter().layout_no_wrap(
        text.clone(),
        egui::FontId::monospace(11.5),
        Color32::PLACEHOLDER,
    );
    let (rect, response) =
        ui.allocate_exact_size(galley.size() + egui::vec2(12.0, 4.0), egui::Sense::click());
    response.widget_info(|| egui::WidgetInfo::labeled(egui::Role::Link, true, &text));
    if ui.is_rect_visible(rect) {
        let painter = ui.painter();
        let fill = if response.hovered() {
            p.accent.gamma_multiply(0.3)
        } else {
            p.tint
        };
        painter.rect_filled(rect, 6.0, fill);
        painter.galley(rect.center() - galley.size() / 2.0, galley, p.accent);
        if response.has_focus() {
            painter.rect_stroke(
                rect.expand(2.0),
                8.0,
                egui::Stroke::new(2.0_f32, p.accent),
                egui::StrokeKind::Inside,
            );
        }
    }
    response.on_hover_cursor(egui::CursorIcon::PointingHand)
}

// A section's label: small caps with the count after it, read as "Sessions (12)".
fn section(ui: &mut egui::Ui, title: &str, n: usize) {
    let p = theme::palette(ui.ctx());
    ui.add_space(14.0);
    let name = format!("{title} ({n})");
    let shown = ui
        .horizontal(|ui| {
            ui.spacing_mut().item_spacing.x = 6.0;
            let caps = ui.label(
                RichText::new(title.to_uppercase())
                    .size(12.0)
                    .family(theme::semibold())
                    .extra_letter_spacing(0.7)
                    .color(p.muted),
            );
            a11y::decorative(ui, caps.id);
            let count = ui.label(RichText::new(n.to_string()).size(12.0).color(p.muted));
            a11y::decorative(ui, count.id);
        })
        .response;
    a11y::heading(&shown, &name, 2);
    ui.add_space(6.0);
}

impl eframe::App for SessionsWindow {
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
        if regained || !self.listed_once {
            self.listed_once = true;
            self.refresh(ctx);
        }
        self.follow_summaries(ctx);
        self.show(ui);
    }
}

const TITLE: &str = "vinoWhisper sessions";

pub fn run(app_id: &str, open: Option<&str>) -> ExitCode {
    let open = open.map(Target::parse);
    let options = eframe::NativeOptions {
        viewport: egui::ViewportBuilder::default()
            .with_title(TITLE)
            .with_app_id(app_id)
            .with_inner_size([820.0, 680.0])
            .with_min_inner_size([340.0, 320.0]),
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
            let mut window = SessionsWindow::with(Settings::load().save_transcripts);
            window.speaker = speak::find_on_path("vinospeak");
            window.kde_header = theme::kde_header();
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
mod tests {
    use super::*;
    use eframe::egui::accesskit::Role;
    use egui_kittest::Harness;
    use egui_kittest::kittest::{NodeT, Queryable};

    // Nothing is listed, read or spawned: `show` never refreshes, and no test presses a button
    // that runs vinowhisper-session.
    struct Under {
        window: SessionsWindow,
        styled: bool,
    }

    fn harness(save_transcripts: bool) -> Harness<'static, Under> {
        Harness::builder().with_size((820.0, 760.0)).build_ui_state(
            |ui, under: &mut Under| {
                let ctx = &ui.ctx().clone();
                if !under.styled {
                    theme::apply(ctx);
                    under.styled = true;
                    ctx.request_repaint();
                    return;
                }
                under.window.show(ui);
            },
            Under {
                window: SessionsWindow::with(save_transcripts),
                styled: false,
            },
        )
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
    fn the_window_is_named() {
        let mut harness = harness(true);
        harness.run();
        harness.get_by_role_and_label(Role::Heading, "Saved sessions");
        assert_eq!(
            harness.root().accesskit_node().label().as_deref(),
            Some(TITLE)
        );
        harness.get_by_role_and_label(Role::Button, "Settings");
    }

    #[test]
    fn each_session_is_listed_with_what_it_can_do() {
        let mut harness = harness(false);
        harness.state_mut().window.state.lock().unwrap().listing = Some(Ok(listing()));
        harness.run();
        harness.get_by_label(
            "The summary model isn't downloaded yet; get it on the Cleanup page of the settings.",
        );
        harness.get_by_role_and_label(Role::Button, "Open settings");
        harness.get_by_role_and_label(Role::Button, "Bundle and summarise 3 transcripts");
        harness.get_by_role_and_label(Role::Heading, "Sessions (2)");
        harness.get_by_role_and_label(Role::Heading, "Local captions talk");
        harness.get_by_label("2026-10-27 14:05 · 42 min · system audio");
        harness.get_by_role_and_label(Role::Heading, "2026-10-26 09:00");
        harness.get_by_label("40 s · microphone");
        harness.get_by_label("Waiting for a summary");
        assert_eq!(
            harness
                .query_all_by_role_and_label(Role::Button, "Transcript")
                .count(),
            2
        );
        // A summary to read, or one to ask for; the rest waits behind More.
        harness.get_by_role_and_label(Role::Button, "Summary");
        harness.get_by_role_and_label(Role::Button, "Summarise");
        assert_eq!(
            harness
                .query_all_by_role_and_label(Role::Button, "More")
                .count(),
            2
        );
        assert!(harness.query_by_label("Delete summary").is_none());
        harness.get_by_label(
            "Saving transcripts is off (settings, Captions page), so new sessions aren't kept.",
        );
    }

    #[test]
    fn deleting_asks_in_the_card_and_cancel_backs_out() {
        let mut harness = harness(true);
        harness.state_mut().window.state.lock().unwrap().listing = Some(Ok(listing()));
        harness.state_mut().window.confirm_delete = Some("/t/b.vwsession".into());
        harness.run();
        harness.get_by_label("Delete this session, transcript and summary?");
        harness.get_by_role_and_label(Role::Button, "Delete the session");
        harness
            .get_by_role_and_label(Role::Button, "Cancel")
            .click();
        harness.run();
        assert!(harness.state().window.confirm_delete.is_none());
        assert!(
            harness
                .query_by_label("Delete this session, transcript and summary?")
                .is_none()
        );
    }

    #[test]
    fn a_summary_opens_with_its_stamps_as_links_and_says_a_model_wrote_it() {
        let mut harness = harness(true);
        {
            let mut state = harness.state_mut().window.state.lock().unwrap();
            state.listing = Some(Ok(listing()));
            state.viewer = Viewer {
                target: Some(Target::parse("/t/b.vwsession#summary")),
                text: Some(Ok(
                    "# Local captions talk\n\nAn overview.\n\n#### Key point (0:02)\n- Runs locally [03:10]\n\n_Machine-written summary._\n".into(),
                )),
                ..Viewer::default()
            };
        }
        harness.run();
        harness.get_by_role_and_label(Role::Button, "All sessions");
        harness.get_by_role_and_label(Role::Button, "Show in folder");
        // Once, as the viewer's heading, not again from the summary's own title.
        assert_eq!(
            harness
                .query_all_by_role_and_label(Role::Heading, "Local captions talk")
                .count(),
            1
        );
        harness.get_by_role_and_label(Role::Heading, "Key point (0:02)");
        harness.get_by_role_and_label(Role::Link, "[00:02]");
        harness.get_by_role_and_label(Role::Link, "[03:10]");
        harness.get_by_label("Machine-written summary.");
        let tab = harness.get_by_role_and_label(Role::Tab, "Summary");
        assert_eq!(tab.accesskit_node().is_selected(), Some(true));
    }

    #[test]
    fn a_transcript_opened_at_a_stamp_marks_that_paragraph() {
        let mut harness = harness(true);
        harness.state_mut().window.state.lock().unwrap().viewer = Viewer {
            target: Some(Target::parse("/t/b.vwsession#transcript@01:30")),
            text: Some(Ok(
                "vinoWhisper transcript, x\n\n[00:00] One.\n\n[01:10] Two.\n".into(),
            )),
            ..Viewer::default()
        };
        harness.run();
        harness.get_by_label("[01:10] Two.");
        assert!(
            harness
                .query_by_label("vinoWhisper transcript, x")
                .is_none()
        );
        assert!(harness.state().window.state.lock().unwrap().viewer.scrolled);
    }

    #[test]
    fn a_session_being_summarised_shows_a_spinner_and_cannot_be_asked_again() {
        let mut harness = harness(true);
        let mut working = listing();
        working.current = Some("/t/a.vwsession".into());
        working.queued = vec!["/t/b.vwsession".into()];
        harness.state_mut().window.state.lock().unwrap().listing = Some(Ok(working));
        harness.run_steps(2);
        harness.get_by_label("Summarising…");
        harness.get_by_label("Queued for a summary");
        let button = harness.get_by_role_and_label(Role::Button, "Summarise");
        assert!(button.accesskit_node().is_disabled());
    }

    fn reading(fragment: &str, text: &str) -> Viewer {
        Viewer {
            target: Some(Target::parse(&format!("/t/b.vwsession#{fragment}"))),
            text: Some(Ok(text.into())),
            ..Viewer::default()
        }
    }

    // The exporter runs on its own thread; step until its answer is in.
    fn until_exported(harness: &mut Harness<'static, Under>) {
        for _ in 0..500 {
            harness.step();
            if harness
                .state()
                .window
                .state
                .lock()
                .unwrap()
                .exported
                .is_some()
            {
                harness.run();
                return;
            }
            std::thread::sleep(Duration::from_millis(5));
        }
        panic!("the export never finished");
    }

    fn exporter_answering(
        harness: &mut Harness<'static, Under>,
        answer: Result<Option<Exported>, String>,
    ) -> Arc<Mutex<Vec<ExportRequest>>> {
        let seen = Arc::new(Mutex::new(Vec::new()));
        let recorded = Arc::clone(&seen);
        harness.state_mut().window.exporter = Arc::new(move |request| {
            recorded.lock().unwrap().push(request.clone());
            answer.clone()
        });
        seen
    }

    fn is_on(harness: &Harness<'static, Under>, label: &str) -> bool {
        harness
            .get_by_role_and_label(Role::RadioButton, label)
            .accesskit_node()
            .toggled()
            == Some(egui::accesskit::Toggled::True)
    }

    #[test]
    fn export_in_the_viewer_follows_the_tab_and_says_where_it_went() {
        let mut harness = harness(true);
        let seen = exporter_answering(
            &mut harness,
            Ok(Some(Exported {
                path: "/d/2026-10-27 Local captions talk.odt".into(),
                state: "written".into(),
                ..Exported::default()
            })),
        );
        {
            let mut state = harness.state_mut().window.state.lock().unwrap();
            state.listing = Some(Ok(listing()));
            state.viewer = reading("summary", "# Local captions talk\n\nText.\n");
        }
        harness.run();
        harness
            .get_by_role_and_label(Role::Button, "Export…")
            .click();
        harness.run();
        harness.get_by_role_and_label(Role::Heading, "Export");
        assert!(is_on(&harness, "Summary"));
        assert!(is_on(&harness, "Markdown"));
        harness
            .get_by_role_and_label(Role::RadioButton, "Document (.odt)")
            .click();
        harness.run();
        harness.get_by_label("For LibreOffice, Word and Google Docs.");
        harness.get_by_role_and_label(Role::Button, "Save…").click();
        until_exported(&mut harness);
        assert_eq!(
            *seen.lock().unwrap(),
            [ExportRequest {
                path: "/t/b.vwsession".into(),
                scope: Scope::Summary,
                format: Format::Odt,
            }]
        );
        assert!(harness.state().window.export_dialog.is_none());
        harness.get_by_label("Exported to /d/2026-10-27 Local captions talk.odt.");
        // The header's and the notice's.
        assert_eq!(
            harness
                .query_all_by_role_and_label(Role::Button, "Show in folder")
                .count(),
            2
        );
        harness
            .get_by_role_and_label(Role::Button, "Dismiss")
            .click();
        harness.run();
        assert!(
            harness
                .state()
                .window
                .state
                .lock()
                .unwrap()
                .exported
                .is_none()
        );
    }

    #[test]
    fn export_from_a_card_offers_only_the_transcript_without_a_summary_and_cancels() {
        let mut harness = harness(true);
        let seen = exporter_answering(&mut harness, Ok(None));
        harness.state_mut().window.state.lock().unwrap().listing = Some(Ok(listing()));
        harness.run();
        // The second card has no summary.
        harness
            .query_all_by_role_and_label(Role::Button, "More")
            .nth(1)
            .unwrap()
            .click();
        harness.run();
        harness.get_by_label("Export…").click();
        harness.run();
        harness.get_by_label("The transcript: this session has no summary yet.");
        assert!(
            harness
                .query_by_role_and_label(Role::RadioButton, "Summary")
                .is_none()
        );
        let dialog = harness.state().window.export_dialog.clone().unwrap();
        assert_eq!(dialog.path, PathBuf::from("/t/a.vwsession"));
        assert_eq!(dialog.scope, Scope::Transcript);
        harness
            .get_by_role_and_label(Role::Button, "Cancel")
            .click();
        harness.run();
        assert!(harness.state().window.export_dialog.is_none());
        assert!(seen.lock().unwrap().is_empty());

        // A card with a summary starts on both.
        harness
            .query_all_by_role_and_label(Role::Button, "More")
            .next()
            .unwrap()
            .click();
        harness.run();
        harness.get_by_label("Export…").click();
        harness.run();
        assert!(is_on(&harness, "Both"));
    }

    #[test]
    fn without_a_save_dialog_the_notice_says_where_it_went_and_what_was_left_out() {
        let mut harness = harness(true);
        exporter_answering(
            &mut harness,
            Ok(Some(Exported {
                path: "/home/me/Documents/vinoWhisper/2026-10-26 Transcript.md".into(),
                summary_missing: true,
                fallback: true,
                ..Exported::default()
            })),
        );
        harness.state_mut().window.state.lock().unwrap().listing = Some(Ok(listing()));
        harness.state_mut().window.export_dialog = Some(ExportDialog {
            path: "/t/a.vwsession".into(),
            scope: Scope::Both,
            format: Format::Markdown,
            has_summary: true,
        });
        harness.run();
        harness.get_by_role_and_label(Role::Button, "Save…").click();
        until_exported(&mut harness);
        harness.get_by_label(
            "There's no save dialog on this desktop, so it's in \
             /home/me/Documents/vinoWhisper/2026-10-26 Transcript.md. \
             The transcript only: there's no summary yet.",
        );
        harness.get_by_role_and_label(Role::Button, "Show in folder");
    }

    #[test]
    fn a_failed_export_says_why_and_a_cancelled_one_says_nothing() {
        let mut harness = harness(true);
        exporter_answering(&mut harness, Err("/d/x.md already exists".into()));
        harness.state_mut().window.state.lock().unwrap().listing = Some(Ok(listing()));
        let dialog = ExportDialog {
            path: "/t/a.vwsession".into(),
            scope: Scope::Transcript,
            format: Format::Markdown,
            has_summary: false,
        };
        harness.state_mut().window.export_dialog = Some(dialog.clone());
        harness.run();
        harness.get_by_role_and_label(Role::Button, "Save…").click();
        until_exported(&mut harness);
        harness.get_by_label("Export didn't work: /d/x.md already exists");

        exporter_answering(&mut harness, Ok(None));
        harness.state_mut().window.export_dialog = Some(dialog);
        harness.run();
        harness.get_by_role_and_label(Role::Button, "Save…").click();
        for _ in 0..500 {
            harness.step();
            if !harness.state().window.state.lock().unwrap().exporting {
                break;
            }
            std::thread::sleep(Duration::from_millis(5));
        }
        harness.run();
        assert!(
            harness
                .state()
                .window
                .state
                .lock()
                .unwrap()
                .exported
                .is_none()
        );
        assert!(
            harness
                .query_by_label_contains("Export didn't work")
                .is_none()
        );
    }

    #[test]
    fn copy_puts_the_part_on_screen_on_the_clipboard() {
        let text = "vinoWhisper transcript, x\n\n[00:00] One.\n";
        let mut harness = harness(true);
        harness.state_mut().window.state.lock().unwrap().viewer = reading("transcript", text);
        harness.run();
        harness.get_by_role_and_label(Role::Button, "Copy").click();
        harness.step();
        assert!(
            harness
                .output()
                .platform_output
                .commands
                .contains(&egui::OutputCommand::CopyText(text.into()))
        );
        harness.run();
        harness.get_by_role_and_label(Role::Button, "Copied");

        // Nothing to copy until the text is in.
        harness.state_mut().window.state.lock().unwrap().viewer = Viewer {
            text: None,
            ..reading("transcript", text)
        };
        harness.run();
        let copy = harness.get_by_role_and_label(Role::Button, "Copy");
        assert!(copy.accesskit_node().is_disabled());
    }

    // A stand-in for vinoSpeak#42's `vinospeak --file BUNDLE --part PART`.
    fn fake_speaker(body: &str) -> PathBuf {
        use std::os::unix::fs::PermissionsExt;
        static N: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);
        let n = N.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        let dir = std::env::temp_dir().join(format!(
            "vinowhisper-fake-vinospeak-{}-{n}",
            std::process::id()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("vinospeak");
        let script = format!("#!/bin/sh\ncd '{}'\n{body}\n", dir.display());
        std::fs::write(&path, script).unwrap();
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755)).unwrap();
        path
    }

    fn viewing_summary(harness: &mut Harness<'static, Under>) {
        let mut state = harness.state_mut().window.state.lock().unwrap();
        state.listing = Some(Ok(listing()));
        state.viewer = Viewer {
            target: Some(Target::parse("/t/b.vwsession#summary")),
            text: Some(Ok("# Local captions talk\n\nText.\n".into())),
            ..Viewer::default()
        };
    }

    fn until(harness: &mut Harness<'static, Under>, done: impl Fn(&Under) -> bool) {
        for _ in 0..400 {
            harness.step();
            if done(harness.state()) {
                harness.run();
                return;
            }
            std::thread::sleep(Duration::from_millis(10));
        }
        panic!("gave up waiting");
    }

    #[test]
    fn read_aloud_is_not_offered_without_vinospeak() {
        let mut harness = harness(true);
        viewing_summary(&mut harness);
        harness.run();
        assert!(harness.query_by_label("Read aloud").is_none());
        harness.state_mut().window.state.lock().unwrap().viewer = Viewer::default();
        harness.run();
        harness
            .query_all_by_role_and_label(Role::Button, "More")
            .next()
            .unwrap()
            .click();
        harness.run();
        harness.get_by_label("Show in folder");
        assert!(harness.query_by_label("Read aloud").is_none());
    }

    #[test]
    fn read_aloud_turns_to_stop_while_it_reads_and_back_when_it_ends() {
        let speaker = fake_speaker("echo \"$@\" > args\nsleep 0.5");
        let args = speaker.with_file_name("args");
        let mut harness = harness(true);
        harness.state_mut().window.speaker = Some(speaker);
        viewing_summary(&mut harness);
        harness.run();
        harness
            .get_by_role_and_label(Role::Button, "Read aloud")
            .click();
        harness.run();
        harness.get_by_role_and_label(Role::Button, "Stop");
        until(&mut harness, |under| under.window.reading.is_none());
        harness.get_by_role_and_label(Role::Button, "Read aloud");
        assert_eq!(
            std::fs::read_to_string(&args).unwrap(),
            "--file /t/b.vwsession --part summary\n"
        );
        assert!(harness.state().window.read_error.is_none());
    }

    #[test]
    fn stop_ends_the_reading_and_whatever_it_started() {
        let speaker = fake_speaker("echo $$ > pid\nsleep 30 &\necho $! > child\nwait");
        let dir = speaker.parent().unwrap().to_path_buf();
        let mut harness = harness(true);
        harness.state_mut().window.speaker = Some(speaker);
        viewing_summary(&mut harness);
        harness.run();
        harness
            .get_by_role_and_label(Role::Button, "Read aloud")
            .click();
        harness.run();
        let pids: Vec<String> = ["pid", "child"]
            .iter()
            .map(|name| {
                for _ in 0..200 {
                    match std::fs::read_to_string(dir.join(name)) {
                        Ok(pid) if pid.ends_with('\n') => return pid.trim().to_owned(),
                        _ => std::thread::sleep(Duration::from_millis(10)),
                    }
                }
                panic!("the fake never wrote {name}");
            })
            .collect();
        harness.get_by_role_and_label(Role::Button, "Stop").click();
        harness.run();
        assert!(harness.state().window.reading.is_none());
        harness.get_by_role_and_label(Role::Button, "Read aloud");
        for pid in pids {
            let proc = PathBuf::from(format!("/proc/{pid}"));
            for _ in 0..300 {
                // Gone, or a zombie its parent has not reaped yet.
                let state = std::fs::read_to_string(proc.join("stat")).unwrap_or_default();
                if state.is_empty() || state.contains(") Z ") {
                    break;
                }
                std::thread::sleep(Duration::from_millis(10));
            }
            let state = std::fs::read_to_string(proc.join("stat")).unwrap_or_default();
            assert!(
                state.is_empty() || state.contains(") Z "),
                "{pid} still runs"
            );
        }
        assert!(harness.state().window.read_error.is_none());
    }

    #[test]
    fn an_old_vinospeak_says_to_update_it() {
        let speaker = fake_speaker(
            "echo 'usage: vinospeak [-h] [--file FILE]' >&2\n\
             echo 'vinospeak: error: unrecognized arguments: --part summary' >&2\nexit 2",
        );
        let mut harness = harness(true);
        harness.state_mut().window.speaker = Some(speaker);
        viewing_summary(&mut harness);
        harness.run();
        harness
            .get_by_role_and_label(Role::Button, "Read aloud")
            .click();
        until(&mut harness, |under| under.window.reading.is_none());
        harness.get_by_label(
            "Reading aloud stopped: this vinoSpeak can't read sessions yet. Update vinoSpeak \
             to read them.",
        );
        harness
            .get_by_role_and_label(Role::Button, "Dismiss")
            .click();
        harness.run();
        assert!(harness.state().window.read_error.is_none());
    }

    #[test]
    fn going_back_to_all_sessions_stops_it_and_a_card_reads_its_summary() {
        let speaker = fake_speaker("echo \"$@\" > args\nexec sleep 30");
        let args = speaker.with_file_name("args");
        let mut harness = harness(true);
        harness.state_mut().window.speaker = Some(speaker);
        viewing_summary(&mut harness);
        harness.run();
        harness
            .get_by_role_and_label(Role::Button, "Read aloud")
            .click();
        harness.run();
        assert!(harness.state().window.reading.is_some());
        harness
            .get_by_role_and_label(Role::Button, "All sessions")
            .click();
        harness.run();
        assert!(harness.state().window.reading.is_none());

        // The first card has a summary; its More menu reads that, then offers to stop.
        harness.state_mut().window.state.lock().unwrap().listing = Some(Ok(listing()));
        harness.run();
        let _ = std::fs::remove_file(&args);
        harness
            .query_all_by_role_and_label(Role::Button, "More")
            .next()
            .unwrap()
            .click();
        harness.run();
        harness.get_by_label("Read aloud").click();
        harness.run();
        until(&mut harness, |_| args.exists());
        assert_eq!(
            std::fs::read_to_string(&args).unwrap(),
            "--file /t/b.vwsession --part summary\n"
        );
        harness
            .query_all_by_role_and_label(Role::Button, "More")
            .next()
            .unwrap()
            .click();
        harness.run();
        harness.get_by_label("Stop reading").click();
        harness.run();
        assert!(harness.state().window.reading.is_none());
    }
}
