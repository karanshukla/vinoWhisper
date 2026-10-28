use std::path::PathBuf;
use std::process::ExitCode;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use eframe::egui::accesskit::Role;
use eframe::egui::{self, Align, Color32, RichText, Stroke};

use crate::forward;
use crate::sessions::{self, Listing, Part, Piece, SummaryLine, Target};
use crate::theme;
use crate::window::{heading, live, section, text_tab};
use vinowhisper_settings::Settings;

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

// Wider than the settings form: this is for reading.
const READING_MAX_WIDTH: f32 = 760.0;
const SCROLL_GAP: f32 = 24.0;

struct SessionsWindow {
    state: Arc<Mutex<SessionsState>>,
    confirm_delete: Option<PathBuf>,
    save_transcripts: bool,
    focused: bool,
    listed_once: bool,
    kde_header: Option<theme::KdeHeader>,
    text_scale: Option<f32>,
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

    fn list(&mut self, ui: &mut egui::Ui, state: &SessionsState) {
        let p = theme::palette(ui.ctx());
        let listing = match &state.listing {
            Some(Ok(listing)) => Some(listing.clone()),
            _ => None,
        };
        if listing.as_ref().is_some_and(|l| !l.model_ready) {
            ui.add_space(10.0);
            ui.horizontal_wrapped(|ui| {
                live(
                    &ui.label(
                        RichText::new(
                            "The summary model isn't downloaded yet; get it on the Cleanup page of the settings.",
                        )
                        .color(ui.visuals().warn_fg_color),
                    ),
                );
                if ui.button("Open settings").clicked() {
                    forward::open_settings("cleanup");
                }
            });
        }
        if !self.save_transcripts {
            ui.add_space(10.0);
            ui.add(
                egui::Label::new(
                    RichText::new(
                        "Saving transcripts is off (settings, Captions page), so new sessions aren't kept.",
                    )
                    .color(p.muted),
                )
                .wrap(),
            );
        }
        if let Some(listing) = listing.as_ref().filter(|l| l.backfill > 0) {
            let count = listing.backfill;
            section(ui, "Older transcripts");
            ui.add(
                egui::Label::new(
                    RichText::new(
                        "Saved before summaries existed, as plain text. Bundling keeps their text as it is.",
                    )
                    .size(12.5)
                    .color(p.muted),
                )
                .wrap(),
            );
            ui.add_space(4.0);
            let label = format!(
                "Bundle and summarise {count} transcript{}",
                if count == 1 { "" } else { "s" }
            );
            if ui
                .add_enabled(!state.busy, egui::Button::new(label))
                .clicked()
            {
                self.act(ui.ctx(), "backfill", None);
            }
        }

        section(ui, "Sessions");
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
                    self.card(ui, entry, listing, state.busy);
                    ui.add_space(8.0);
                }
            }
        }
    }

    fn card(&mut self, ui: &mut egui::Ui, entry: &sessions::Entry, listing: &Listing, busy: bool) {
        let p = theme::palette(ui.ctx());
        egui::Frame::NONE
            .stroke(Stroke::new(1.0_f32, p.rule))
            .corner_radius(4.0)
            .inner_margin(egui::Margin::symmetric(12, 10))
            .show(ui, |ui| {
                ui.set_width(ui.available_width());
                let working = listing.current.as_ref() == Some(&entry.path);
                let waiting = working || listing.queued.contains(&entry.path);
                // The title leads once there is one; until then the date stands in for it.
                let title = entry.title.clone().filter(|_| entry.error.is_none());
                let name = title.clone().unwrap_or_else(|| entry.when());
                heading(
                    &ui.label(RichText::new(&name).family(theme::bold())),
                    &name,
                    3,
                );
                let mut facts = Vec::new();
                if title.is_some() {
                    facts.push(entry.when());
                }
                facts.extend(entry.length());
                facts.extend(entry.source.clone());
                ui.label(RichText::new(facts.join(" · ")).color(p.muted));
                let status = match &entry.error {
                    Some(error) => Some((error.clone(), ui.visuals().error_fg_color)),
                    None if waiting || !entry.summary => {
                        Some((listing.status(entry).to_owned(), p.muted))
                    }
                    None => None,
                };
                if let Some((status, color)) = status {
                    ui.horizontal_wrapped(|ui| {
                        if working {
                            ui.add(egui::Spinner::new());
                        }
                        live(&ui.add(egui::Label::new(RichText::new(status).color(color)).wrap()));
                    });
                }
                if !entry.verified && entry.error.is_none() {
                    ui.label(
                        RichText::new("Changed since it was written (the hashes don't match).")
                            .color(ui.visuals().warn_fg_color),
                    );
                }
                ui.add_space(6.0);
                let path = entry.path.clone();
                if self.confirm_delete.as_ref() == Some(&path) {
                    ui.horizontal_wrapped(|ui| {
                        ui.label("Delete this session, transcript and summary?");
                        if ui.button("Delete the session").clicked() {
                            self.confirm_delete = None;
                            self.act(ui.ctx(), "delete", Some(path.clone()));
                        }
                        if ui.button("Cancel").clicked() {
                            self.confirm_delete = None;
                        }
                    });
                    return;
                }
                ui.horizontal_wrapped(|ui| {
                    let readable = entry.error.is_none();
                    let open = |part| Target {
                        path: path.clone(),
                        part,
                        at_s: None,
                    };
                    if ui
                        .add_enabled(readable, egui::Button::new("Transcript"))
                        .clicked()
                    {
                        self.open(ui.ctx(), open(Part::Transcript));
                    }
                    if entry.summary {
                        if ui
                            .add_enabled(readable, egui::Button::new("Summary"))
                            .clicked()
                        {
                            self.open(ui.ctx(), open(Part::Summary));
                        }
                    } else if ui
                        .add_enabled(
                            readable && !busy && !waiting,
                            egui::Button::new("Summarise"),
                        )
                        .clicked()
                    {
                        self.act(ui.ctx(), "summarise", Some(path.clone()));
                    }
                    ui.menu_button("More", |ui| {
                        if entry.summary
                            && ui
                                .add_enabled(
                                    readable && !busy && !waiting,
                                    egui::Button::new("Summarise again"),
                                )
                                .clicked()
                        {
                            self.act(ui.ctx(), "summarise", Some(path.clone()));
                        }
                        if entry.summary
                            && ui
                                .add_enabled(!busy, egui::Button::new("Delete summary"))
                                .clicked()
                        {
                            self.act(ui.ctx(), "delete-summary", Some(path.clone()));
                        }
                        if ui.button("Show in folder").clicked() {
                            sessions::show_in_folder(&path);
                        }
                        ui.separator();
                        if ui
                            .add_enabled(!busy, egui::Button::new("Delete…"))
                            .clicked()
                        {
                            self.confirm_delete = Some(path.clone());
                        }
                    });
                });
            });
    }

    fn viewer(&mut self, ui: &mut egui::Ui, state: &SessionsState) {
        let p = theme::palette(ui.ctx());
        let Some(target) = state.viewer.target.clone() else {
            return;
        };
        ui.add_space(10.0);
        if ui.button("All sessions").clicked() {
            self.state.lock().unwrap().viewer = Viewer::default();
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
        ui.add_space(14.0);
        heading(
            &ui.label(RichText::new(&name).family(theme::bold()).size(16.0)),
            &name,
            2,
        );
        if let Some(entry) = &entry {
            let mut facts = Vec::new();
            if title.is_some() {
                facts.push(entry.when());
            }
            facts.extend(entry.length());
            facts.extend(entry.source.clone());
            ui.label(RichText::new(facts.join(" · ")).color(p.muted));
        }
        ui.add_space(10.0);
        let colors = theme::Header {
            bg: p.bg,
            text: p.text,
            muted: p.muted,
        };
        ui.horizontal(|ui| {
            ui.spacing_mut().item_spacing.x = 18.0;
            for (part, label) in [(Part::Transcript, "Transcript"), (Part::Summary, "Summary")] {
                let enabled = part == Part::Transcript || entry.as_ref().is_none_or(|e| e.summary);
                if text_tab(ui, label, target.part == part, enabled, colors).clicked()
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
        });
        ui.add_space(12.0);
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
            Part::Summary => self.summary(ui, &target, &text, title.as_deref()),
            Part::Transcript => self.transcript(ui, state, &target, &text),
        }
    }

    // A line with stamps in it, each a link into the transcript.
    // Returns the first piece drawn, for a heading to be marked on.
    fn stamped(
        &self,
        ui: &mut egui::Ui,
        target: &Target,
        line: &str,
        bold: bool,
    ) -> egui::Response {
        let styled = |text: &str| {
            let text = RichText::new(text);
            if bold {
                text.family(theme::bold())
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
                        let stamp = sessions::stamp(seconds);
                        if ui.link(format!("[{stamp}]")).clicked() {
                            self.open(
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
            first
        })
        .inner
        .unwrap_or_else(|| ui.response())
    }

    fn summary(&self, ui: &mut egui::Ui, target: &Target, text: &str, title: Option<&str>) {
        let p = theme::palette(ui.ctx());
        let mut first_heading = true;
        for line in text.lines() {
            match sessions::summary_line(line) {
                SummaryLine::Blank => ui.add_space(6.0),
                // The title is already the viewer's heading.
                SummaryLine::Heading(heading_text)
                    if first_heading && Some(heading_text.as_str()) == title =>
                {
                    first_heading = false;
                }
                SummaryLine::Heading(heading_text) => {
                    first_heading = false;
                    ui.add_space(4.0);
                    let shown = self.stamped(ui, target, &heading_text, true);
                    heading(&shown, &heading_text, 3);
                }
                SummaryLine::Bullet(item) => {
                    ui.horizontal_top(|ui| {
                        ui.spacing_mut().item_spacing.x = 8.0;
                        ui.label(RichText::new("•").color(p.muted));
                        ui.vertical(|ui| {
                            self.stamped(ui, target, &item, false);
                        });
                    });
                }
                SummaryLine::Note(note) => {
                    ui.add_space(6.0);
                    ui.add(egui::Label::new(RichText::new(note).size(12.5).color(p.muted)).wrap());
                }
                SummaryLine::Text(line) => {
                    self.stamped(ui, target, &line, false);
                }
            }
        }
    }

    fn transcript(&self, ui: &mut egui::Ui, state: &SessionsState, target: &Target, text: &str) {
        let p = theme::palette(ui.ctx());
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
            let body = egui::TextFormat {
                font_id: egui::FontId::proportional(14.0),
                color: p.text,
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
                        color: p.muted,
                        ..body.clone()
                    },
                );
                job.append(words, 0.0, body);
            } else {
                job.append(words, 0.0, body);
            }
            let shown = egui::Frame::NONE
                .fill(if marked {
                    theme::selected_fill(p)
                } else {
                    Color32::TRANSPARENT
                })
                .corner_radius(4.0)
                .inner_margin(egui::Margin::symmetric(6, 4))
                // Out by the padding, so the text lines up with the heading above it.
                .outer_margin(egui::Margin {
                    left: -6,
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
        let state = self.state.lock().unwrap().clone();

        egui::Panel::top("header")
            .frame(
                egui::Frame::NONE
                    .fill(header.bg)
                    .inner_margin(egui::Margin::symmetric(16, 10)),
            )
            .show(ui, |ui| {
                // As the settings window's, so the two headers are one height.
                ui.horizontal_wrapped(|ui| {
                    ui.add_space(8.0);
                    let title = ui.label(
                        RichText::new("Saved sessions")
                            .family(theme::bold())
                            .size(16.0)
                            .color(header.text),
                    );
                    heading(&title, "Saved sessions", 1);
                });
            });

        egui::CentralPanel::default()
            .frame(
                egui::Frame::NONE
                    .fill(p.bg)
                    // No right margin: the scroll bar belongs on the window's edge.
                    .inner_margin(egui::Margin {
                        left: 24,
                        right: 0,
                        top: 4,
                        bottom: 4,
                    }),
            )
            .show(ui, |ui| {
                ui.spacing_mut().scroll = egui::style::ScrollStyle::solid();
                let scrolled = egui::ScrollArea::vertical()
                    .auto_shrink([false, false])
                    .scroll_bar_visibility(egui::scroll_area::ScrollBarVisibility::AlwaysVisible)
                    .scroll_source(egui::scroll_area::ScrollSource {
                        drag: egui::scroll_area::DragScroll::Never,
                        ..Default::default()
                    })
                    .show(ui, |ui| {
                        ui.set_max_width(
                            (ui.available_width() - SCROLL_GAP).min(READING_MAX_WIDTH),
                        );
                        if state.viewer.target.is_some() {
                            self.viewer(ui, &state);
                        } else {
                            self.list(ui, &state);
                        }
                        ui.add_space(12.0);
                    });
                ctx.accesskit_node_builder(scrolled.id.with(1usize), |node| {
                    node.set_role(Role::ScrollBar);
                    node.set_label("Scroll saved sessions");
                    node.set_orientation(egui::accesskit::Orientation::Vertical);
                });
            });
    }
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
            .with_inner_size([820.0, 760.0])
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
                    theme::apply_for_tests(ctx);
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
                scrolled: false,
            };
        }
        harness.run();
        harness.get_by_role_and_label(Role::Button, "All sessions");
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
            scrolled: false,
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
}
