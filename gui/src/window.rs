use std::process::ExitCode;

use eframe::egui::{self, Align, FontId, Layout, RichText, Sense, Stroke, TextStyle};

use crate::theme;
use vinowhisper_settings::{
    Language, Position, Settings, Source, Speech, Task, TextSize, multilingual_installed,
};

#[derive(Clone, Copy, PartialEq, Eq)]
enum Page {
    Shortcuts,
    Captions,
    Speech,
}

impl Page {
    const ALL: [Page; 3] = [Page::Shortcuts, Page::Captions, Page::Speech];

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
        }
    }

    fn lede(self) -> &'static str {
        match self {
            Page::Shortcuts => "How you start captions and dictation from anywhere.",
            Page::Captions => "How the floating captions look and where they listen.",
            Page::Speech => "The language the model hears, and whether it translates.",
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

fn combo<T: Copy + PartialEq>(
    ui: &mut egui::Ui,
    id: &str,
    value: &mut T,
    all: &[T],
    label: fn(T) -> &'static str,
) -> bool {
    let before = *value;
    egui::ComboBox::from_id_salt(id)
        .selected_text(label(*value))
        .show_ui(ui, |ui| {
            for &option in all {
                ui.selectable_value(value, option, label(option));
            }
        });
    *value != before
}

/// Same measure as the landing page's prose: a settings row never runs wider than this.
const CONTENT_WIDTH: f32 = 600.0;

struct SettingsWindow {
    page: Page,
    settings: Settings,
    speech: Speech,
    multilingual: bool,
    saved: Option<&'static str>,
}

impl SettingsWindow {
    fn new(page: Option<Page>) -> Self {
        SettingsWindow {
            page: page.unwrap_or(Page::Shortcuts),
            settings: Settings::load(),
            speech: Speech::load(),
            multilingual: multilingual_installed(),
            saved: None,
        }
    }

    fn shortcuts(&mut self, ui: &mut egui::Ui) -> bool {
        let mut changed = false;
        row(
            ui,
            "Captions shortcut",
            "Toggles the floating captions. Written the way the desktop portal reads it, for example LOGO+ALT+C.",
            |ui| {
                let response = ui.add(
                    egui::TextEdit::singleline(&mut self.settings.shortcut)
                        .font(TextStyle::Monospace)
                        .desired_width(260.0),
                );
                // Only a finished edit is saved, so a half-typed trigger never reaches the tray.
                if response.lost_focus() {
                    if self.settings.shortcut.trim().is_empty() {
                        self.settings.shortcut = vinowhisper_settings::DEFAULT_SHORTCUT.to_owned();
                    }
                    changed = true;
                }
            },
        );
        changed
    }

    fn captions(&mut self, ui: &mut egui::Ui) -> bool {
        let mut changed = false;
        row(ui, "Listen to", "What the captions follow.", |ui| {
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
            "Position",
            "Where the caption box sits above other windows.",
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
        row(ui, "Text size", "How large the captions are drawn.", |ui| {
            changed |= combo(
                ui,
                "size",
                &mut self.settings.size,
                &TextSize::ALL,
                size_label,
            );
        });
        row(
            ui,
            "Transcripts",
            "Write each session to a file, so you can read it back. Off until you turn it on.",
            |ui| {
                changed |= ui
                    .checkbox(&mut self.settings.save_transcripts, "Save transcripts")
                    .changed();
            },
        );
        row(
            ui,
            "Tray icon",
            "The tray unloads after this long without use. 0 keeps it up all the time.",
            |ui| {
                changed |= ui
                    .add(
                        egui::DragValue::new(&mut self.settings.tray_idle_minutes)
                            .range(0..=1440)
                            .suffix(" min idle"),
                    )
                    .changed();
            },
        );
        changed
    }

    fn speech(&mut self, ui: &mut egui::Ui) -> bool {
        let mut changed = false;
        if !self.multilingual {
            let p = theme::palette(ui.ctx());
            ui.label(
                RichText::new(
                    "The multilingual model is not installed, so only English works. Run vinowhisper-setup to add it.",
                )
                .color(p.muted),
            );
            ui.add_space(8.0);
        }
        let multilingual = self.multilingual;
        row(
            ui,
            "Language",
            "What is being said. Detect automatically picks per sentence.",
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
            "Translation",
            "Captions arrive in English whatever was spoken. English audio has nothing to translate.",
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

/// A setting set the way the landing page sets a feature: a hairline, a bold name, the
/// explanation in muted text, then the control.
fn row(ui: &mut egui::Ui, title: &str, help: &str, control: impl FnOnce(&mut egui::Ui)) {
    let p = theme::palette(ui.ctx());
    ui.add_space(4.0);
    let rule = ui.available_rect_before_wrap().left_top();
    let width = ui.available_width().min(CONTENT_WIDTH);
    ui.painter().hline(
        rule.x..=rule.x + width,
        rule.y,
        Stroke::new(1.0_f32, p.rule),
    );
    ui.add_space(14.0);
    ui.scope(|ui| {
        ui.set_max_width(width);
        ui.label(RichText::new(title).family(theme::bold()).size(16.0));
        ui.label(RichText::new(help).size(14.0).color(p.muted));
        ui.add_space(2.0);
        control(ui);
    });
    ui.add_space(16.0);
}

/// A header link: muted until it is the page you are on, then underlined in the link green.
fn nav_item(ui: &mut egui::Ui, page: Page, current: &mut Page) {
    let p = theme::palette(ui.ctx());
    let active = *current == page;
    let galley = ui.painter().layout_no_wrap(
        page.title().to_owned(),
        if active {
            FontId::new(15.0, theme::bold())
        } else {
            FontId::proportional(15.0)
        },
        if active { p.text } else { p.muted },
    );
    let (rect, response) =
        ui.allocate_exact_size(galley.size() + egui::vec2(0.0, 6.0), Sense::click());
    let hovered = response.hovered();
    let text_pos = rect.left_top();
    ui.painter().galley(
        text_pos,
        galley.clone(),
        if hovered && !active { p.text } else { p.muted },
    );
    if active {
        let y = rect.bottom();
        ui.painter()
            .hline(rect.left()..=rect.right(), y, Stroke::new(2.0_f32, p.link));
    }
    if response.clicked() {
        *current = page;
    }
    response.on_hover_cursor(egui::CursorIcon::PointingHand);
}

impl eframe::App for SettingsWindow {
    fn update(&mut self, ctx: &egui::Context, _frame: &mut eframe::Frame) {
        let p = theme::palette(ctx);
        let mut saved_settings = false;
        let mut saved_speech = false;

        egui::TopBottomPanel::top("header")
            .frame(
                egui::Frame::NONE
                    .fill(p.bg)
                    .inner_margin(egui::Margin::symmetric(24, 16)),
            )
            .show(ctx, |ui| {
                ui.horizontal(|ui| {
                    ui.label(
                        RichText::new("vinoWhisper")
                            .family(theme::bold())
                            .size(17.0)
                            .color(p.text),
                    );
                    ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                        ui.spacing_mut().item_spacing.x = 20.0;
                        for page in Page::ALL.into_iter().rev() {
                            nav_item(ui, page, &mut self.page);
                        }
                    });
                });
            });

        egui::TopBottomPanel::bottom("footer")
            .frame(
                egui::Frame::NONE
                    .fill(p.bg)
                    .inner_margin(egui::Margin::symmetric(24, 12)),
            )
            .show(ctx, |ui| {
                ui.horizontal(|ui| {
                    match self.saved {
                        Some(text) => ui.label(RichText::new(text).color(p.link).size(14.0)),
                        None => ui.label(
                            RichText::new("Changes are saved as you make them.")
                                .color(p.muted)
                                .size(14.0),
                        ),
                    };
                    ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                        let path = Settings::path();
                        ui.label(
                            RichText::new(path.display().to_string())
                                .monospace()
                                .size(12.5)
                                .color(p.muted),
                        );
                    });
                });
            });

        egui::CentralPanel::default()
            .frame(
                egui::Frame::NONE
                    .fill(p.bg)
                    .stroke(Stroke::NONE)
                    .inner_margin(egui::Margin::symmetric(24, 8)),
            )
            .show(ctx, |ui| {
                egui::ScrollArea::vertical()
                    .auto_shrink([false, false])
                    .show(ui, |ui| {
                        ui.add_space(20.0);
                        ui.label(
                            RichText::new(self.page.title())
                                .family(theme::bold())
                                .size(30.0),
                        );
                        ui.add_space(4.0);
                        ui.label(RichText::new(self.page.lede()).size(16.0).color(p.muted));
                        ui.add_space(20.0);
                        match self.page {
                            Page::Shortcuts => saved_settings = self.shortcuts(ui),
                            Page::Captions => saved_settings = self.captions(ui),
                            Page::Speech => saved_speech = self.speech(ui),
                        }
                    });
            });

        if saved_settings {
            self.settings.save();
            self.saved = Some("Saved. The tray picks this up when it next starts.");
        }
        if saved_speech {
            self.speech.save();
            self.saved = Some("Saved. The server reads the language the next time it loads.");
        }
    }
}

pub fn run(app_id: &str, page: Option<&str>) -> ExitCode {
    let page = page.and_then(Page::parse);
    let options = eframe::NativeOptions {
        viewport: egui::ViewportBuilder::default()
            .with_title("vinoWhisper settings")
            .with_app_id(app_id)
            .with_inner_size([720.0, 680.0])
            .with_min_inner_size([520.0, 420.0]),
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
