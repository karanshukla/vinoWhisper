use std::process::ExitCode;

use eframe::egui;
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

struct SettingsWindow {
    page: Page,
    settings: Settings,
    speech: Speech,
    multilingual: bool,
}

impl SettingsWindow {
    fn new() -> Self {
        SettingsWindow {
            page: Page::Shortcuts,
            settings: Settings::load(),
            speech: Speech::load(),
            multilingual: multilingual_installed(),
        }
    }

    fn shortcuts(&mut self, ui: &mut egui::Ui) -> bool {
        let mut changed = false;
        ui.label("Captions shortcut");
        let response = ui.text_edit_singleline(&mut self.settings.shortcut);
        // Only a finished edit is saved, so a half-typed trigger never reaches the tray.
        if response.lost_focus() {
            if self.settings.shortcut.trim().is_empty() {
                self.settings.shortcut = vinowhisper_settings::DEFAULT_SHORTCUT.to_owned();
            }
            changed = true;
        }
        ui.weak("Written as the desktop portal reads it, for example LOGO+ALT+C.");
        changed
    }

    fn captions(&mut self, ui: &mut egui::Ui) -> bool {
        let mut changed = false;
        egui::Grid::new("captions").num_columns(2).show(ui, |ui| {
            ui.label("Listen to");
            changed |= combo(
                ui,
                "source",
                &mut self.settings.source,
                &Source::ALL,
                source_label,
            );
            ui.end_row();
            ui.label("Position");
            changed |= combo(
                ui,
                "position",
                &mut self.settings.position,
                &Position::ALL,
                position_label,
            );
            ui.end_row();
            ui.label("Text size");
            changed |= combo(
                ui,
                "size",
                &mut self.settings.size,
                &TextSize::ALL,
                size_label,
            );
            ui.end_row();
        });
        changed |= ui
            .checkbox(&mut self.settings.save_transcripts, "Save transcripts")
            .changed();
        ui.horizontal(|ui| {
            ui.label("Hide the tray icon after");
            changed |= ui
                .add(
                    egui::DragValue::new(&mut self.settings.tray_idle_minutes)
                        .range(0..=1440)
                        .suffix(" min idle"),
                )
                .changed();
        });
        ui.weak("0 keeps the tray icon up all the time.");
        changed
    }

    fn speech(&mut self, ui: &mut egui::Ui) -> bool {
        let mut changed = false;
        if !self.multilingual {
            ui.colored_label(
                ui.visuals().warn_fg_color,
                "The multilingual model is not installed, so only English works. Run vinowhisper-setup.",
            );
        }
        ui.add_enabled_ui(self.multilingual, |ui| {
            egui::Grid::new("speech").num_columns(2).show(ui, |ui| {
                ui.label("Language");
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
                ui.end_row();
            });
            let foreign = self.speech.language != Language::En;
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
        });
        changed
    }
}

impl eframe::App for SettingsWindow {
    fn update(&mut self, ctx: &egui::Context, _frame: &mut eframe::Frame) {
        let mut saved_settings = false;
        let mut saved_speech = false;
        egui::SidePanel::left("pages")
            .resizable(false)
            .show(ctx, |ui| {
                for page in Page::ALL {
                    ui.selectable_value(&mut self.page, page, page.title());
                }
            });
        egui::CentralPanel::default().show(ctx, |ui| {
            ui.heading(self.page.title());
            ui.add_space(8.0);
            match self.page {
                Page::Shortcuts => saved_settings = self.shortcuts(ui),
                Page::Captions => saved_settings = self.captions(ui),
                Page::Speech => saved_speech = self.speech(ui),
            }
        });
        if saved_settings {
            self.settings.save();
        }
        if saved_speech {
            self.speech.save();
        }
    }
}

pub fn run(app_id: &str) -> ExitCode {
    let options = eframe::NativeOptions {
        viewport: egui::ViewportBuilder::default()
            .with_title("vinoWhisper settings")
            .with_app_id(app_id)
            .with_inner_size([560.0, 380.0]),
        ..Default::default()
    };
    let result = eframe::run_native(
        "vinoWhisper settings",
        options,
        Box::new(|_cc| Ok(Box::new(SettingsWindow::new()))),
    );
    match result {
        Ok(()) => ExitCode::SUCCESS,
        Err(err) => {
            eprintln!("[vinowhisper-gui] {err}");
            ExitCode::FAILURE
        }
    }
}
