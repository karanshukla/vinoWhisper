use std::process::ExitCode;

use eframe::egui::{self, Align, Color32, FontId, Layout, RichText, Sense, Stroke, TextStyle};

use crate::forward;
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
    multilingual: bool,
    focused: bool,
    kde_header: Option<theme::KdeHeader>,
}

impl SettingsWindow {
    fn new(page: Option<Page>) -> Self {
        SettingsWindow {
            page: page.unwrap_or(Page::Shortcuts),
            settings: Settings::load(),
            speech: Speech::load(),
            multilingual: multilingual_installed(),
            focused: true,
            kde_header: theme::kde_header(),
        }
    }

    fn shortcuts(&mut self, ui: &mut egui::Ui) -> bool {
        let mut changed = false;
        section(ui, "Captions");
        row(
            ui,
            "Preferred shortcut:",
            "Asked of the desktop the first time captions are bound. Once bound, change it from the tray menu or your desktop's keyboard settings.",
            |ui| {
                let response = ui.add(
                    egui::TextEdit::singleline(&mut self.settings.shortcut)
                        .font(TextStyle::Monospace)
                        .desired_width(COMBO_WIDTH),
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
                changed |= ui
                    .add(
                        egui::DragValue::new(&mut self.settings.tray_idle_minutes)
                            .range(0..=1440)
                            .suffix(" min"),
                    )
                    .changed();
            },
        );
        changed
    }

    fn speech(&mut self, ui: &mut egui::Ui) -> bool {
        let mut changed = false;
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
            ui.label(RichText::new(help).size(12.5).color(p.muted));
        }
    };
    if ui.available_width() >= TWO_COLUMNS_FROM {
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
                control(ui);
                help(ui);
            });
        });
    } else {
        ui.label(label);
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
    fn update(&mut self, ctx: &egui::Context, _frame: &mut eframe::Frame) {
        let focused = ctx.input(|input| input.focused);
        if focused && !self.focused {
            // The desktop's light/dark switch may have flipped while we were in the background.
            theme::follow_desktop(ctx);
            self.kde_header = theme::kde_header();
        }
        self.focused = focused;
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
                        ui.set_max_width(FORM_MAX_WIDTH);
                        match self.page {
                            Page::Shortcuts => saved_settings = self.shortcuts(ui),
                            Page::Captions => saved_settings = self.captions(ui),
                            Page::Speech => saved_speech = self.speech(ui),
                        }
                        ui.add_space(12.0);
                    });
            });

        if saved_settings {
            self.settings.save();
            forward::reload_tray();
        }
        if saved_speech {
            self.speech.save();
            forward::reload_tray();
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
