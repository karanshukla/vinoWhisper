use std::path::PathBuf;
use std::process::ExitCode;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use eframe::egui::{self, Align, Layout, RichText};
use vino_ui::a11y;
use vino_ui::icons::Icon;
use vino_ui::theme;
use vino_ui::widgets::{self, Button, ChipKind, Column, Row, Tone};

use crate::forward::{self, Shortcuts};
use crate::model::{self, Line, ModelState, Status, Tool};
use crate::sessions;
use vinowhisper_settings::{
    Cleanup, CleanupMode, Language, Position, Settings, Source, Speech, SpeechModel, Summaries,
    Task, TextSize, multilingual_installed, vocabulary,
};

mod captions_page;
mod cleanup_page;
mod controls;
mod labels;
mod models;
mod sessions_page;
mod shortcuts_page;
mod speech_page;

use controls::{COMBO_WIDTH, combo, number, segmented};
use labels::*;

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

    fn max_width(self) -> f32 {
        match self {
            Page::Shortcuts | Page::About => widgets::CONTENT_MAX,
            Page::Captions | Page::Speech | Page::Cleanup | Page::Sessions => widgets::WIDE_MAX,
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
    whisper_model: Arc<Mutex<ModelState>>,
    confirm_delete_whisper: bool,
    confirm_delete_source: bool,
    // A delete was started: its effects are applied once the status says Whisper is gone.
    whisper_delete_pending: bool,
    canary_delete_pending: bool,
    multilingual: bool,
    canary: bool,
    focused: bool,
    kde_header: Option<theme::KdeHeader>,
    // The window's buttons and edges are drawn here, not by the desktop (vino-ui's `window`).
    app_title_bar: bool,
    // Zoom follows the desktop's factor only when it changes, so a Ctrl + or - since then is kept.
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
    settings_dirty: bool,
    // vocabulary.txt, written on each add and remove: the server reads it per dictation.
    vocabulary_path: PathBuf,
    vocabulary: Vec<String>,
    new_term: String,
    vocabulary_error: Option<String>,
    // The sessions window's app menu entry, and why the last change to it failed.
    apps_data: PathBuf,
    in_app_menu: bool,
    app_menu_error: Option<String>,
    // From the session listing, read when the Sessions page is shown.
    model_ready: Arc<Mutex<Option<bool>>>,
    updater: vino_ui::update::Updater,
}

// vinowhisper-upgrade for a release install, vinowhisper-flatpak-update for a release Flatpak, else a link.
fn updater() -> vino_ui::update::Updater {
    let updater = vino_ui::update::Updater::new(vino_ui::App::Whisper, env!("CARGO_PKG_VERSION"));
    if vinowhisper_settings::sandbox::flatpak_id().is_some() {
        return if vinowhisper_settings::sandbox::is_release() {
            updater.with_command(["vinowhisper-flatpak-update"])
        } else {
            updater
        };
    }
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
        window.vocabulary_path = vocabulary::path();
        window.vocabulary = vocabulary::load_from(&window.vocabulary_path);
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
            whisper_model: Arc::new(Mutex::new(ModelState::default())),
            confirm_delete_whisper: false,
            confirm_delete_source: false,
            whisper_delete_pending: false,
            canary_delete_pending: false,
            multilingual: false,
            canary: false,
            focused: true,
            kde_header: None,
            app_title_bar: false,
            text_scale: None,
            shortcuts: Arc::new(Mutex::new(Shortcuts::Asking)),
            shown: None,
            refused: Arc::new(Mutex::new(None)),
            notice: None,
            speech_note: None,
            summaries: Summaries::default(),
            apps_data: vinowhisper_settings::apps_data_home(),
            in_app_menu: false,
            app_menu_error: None,
            summaries_dirty: false,
            settings_dirty: false,
            vocabulary_path: PathBuf::new(),
            vocabulary: Vec::new(),
            new_term: String::new(),
            vocabulary_error: None,
            model_ready: Arc::new(Mutex::new(None)),
            updater: updater(),
        }
    }
}

fn tab_name(page: Page) -> String {
    if page.beta() {
        format!("{} (beta)", page.title())
    } else {
        page.title().to_owned()
    }
}

impl eframe::App for SettingsWindow {
    fn raw_input_hook(&mut self, _ctx: &egui::Context, raw: &mut egui::RawInput) {
        vino_ui::input::faster_touchpad(raw);
    }

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
            self.refresh_model(ctx, Tool::Whisper);
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
        if self.app_title_bar {
            vino_ui::window::edges(ui);
        }

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
    // Draws and says which files changed; `ui` does the saving, so tests can drive this alone.
    fn show(&mut self, ui: &mut egui::Ui) -> (bool, bool) {
        let ctx = &ui.ctx().clone();
        ctx.accesskit_node_builder(egui::accesskit_root_id(), |node| node.set_label(TITLE));
        let p = theme::palette(ctx);
        let header = theme::header(ctx, self.kde_header.as_ref(), self.focused);
        let window = ctx.content_rect().width();
        let wide = window >= widgets::SIDEBAR_FROM;
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
                .exact_size(widgets::sidebar_width(window))
                .frame(widgets::sidebar_frame(p))
                .show(ui, |ui| {
                    ui.spacing_mut().item_spacing.y = 2.0;
                    // On the nav items' icon column, which vino-ui centres the window bar's row on.
                    let brand = ui
                        .scope(|ui| {
                            ui.style_mut().interaction.selectable_labels = false;
                            widgets::sidebar_brand(
                                ui,
                                Some(vino_ui::logo::whisper),
                                "vinoWhisper",
                                "Settings",
                            );
                        })
                        .response
                        .rect;
                    if self.app_title_bar {
                        // After the brand, so its name drags the window.
                        let clip = ui.clip_rect();
                        vino_ui::window::drag(
                            ui,
                            egui::Rect::from_x_y_ranges(
                                clip.x_range(),
                                clip.top()..=brand.bottom(),
                            ),
                            "brand",
                        );
                    }
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
                    if self.app_title_bar {
                        vino_ui::window::drag(ui, ui.clip_rect(), "header");
                    }
                    ui.horizontal(|ui| {
                        vino_ui::logo::whisper(ui, 22.0);
                        ui.add(
                            egui::Label::new(
                                RichText::new("vinoWhisper")
                                    .size(15.0)
                                    .family(theme::bold())
                                    .color(header.text),
                            )
                            .selectable(false),
                        );
                        if self.app_title_bar {
                            vino_ui::window::controls(ui);
                        }
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

        // A wide window's page title sits in a bar of its own, which the window is dragged by.
        let bar = wide && self.app_title_bar;
        let page = self.page;
        let title = |ui: &mut egui::Ui| {
            ui.spacing_mut().item_spacing.x = 10.0;
            widgets::title(ui, page.title());
            if page.beta() {
                let tag = widgets::tag(ui, Tone::Warn, "Beta");
                a11y::decorative(ui, tag.id);
            }
        };
        if bar {
            vino_ui::window::bar(ui, widgets::page_inset(window), title, |_| {});
        }

        egui::CentralPanel::default()
            .frame(
                egui::Frame::NONE
                    .fill(p.bg)
                    // No right margin: the scroll bar belongs on the window's edge.
                    .inner_margin(egui::Margin {
                        left: if wide {
                            widgets::page_inset(window) as i8
                        } else {
                            16
                        },
                        right: 0,
                        top: match (wide, bar) {
                            // The same 15 from title to content as in the scroll area.
                            (_, true) => 2,
                            (true, false) => 20,
                            (false, false) => 12,
                        },
                        bottom: 0,
                    }),
            )
            .show(ui, |ui| {
                widgets::page_scroll(ui, self.page.title(), self.page.max_width(), |ui| {
                    if !bar {
                        ui.horizontal(title);
                    }
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
        saved_settings |= std::mem::take(&mut self.settings_dirty);
        (saved_settings, saved_speech)
    }
}

const TITLE: &str = "vinoWhisper settings";

pub fn run(app_id: &str, page: Option<&str>) -> ExitCode {
    let page = page.and_then(Page::parse);
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
        // vsync off: a swap waits on a frame callback KWin holds back, and pings go unanswered (docs/tray.md).
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
            window.app_title_bar = app_title_bar;
            // Before the first frame: a window opened in the background would draw its first frames small.
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
