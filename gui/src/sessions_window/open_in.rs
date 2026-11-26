use super::*;

impl SessionsWindow {
    pub(super) fn open_with(
        &mut self,
        ctx: &egui::Context,
        path: PathBuf,
        part: Part,
        choice: OpenIn,
    ) {
        if choice != self.open_in {
            self.open_in = choice;
            let mut settings = Settings::load_from(&self.settings_path);
            settings.open_in = choice;
            if let Err(err) = settings.save_to(&self.settings_path) {
                eprintln!(
                    "[vinowhisper-gui] could not save {}: {err}",
                    self.settings_path.display()
                );
            }
        }
        let request = ExportRequest {
            path,
            scope: part.into(),
            format: sessions::format_for(choice),
        };
        let state = Arc::clone(&self.state);
        let opener = Arc::clone(&self.opener);
        let ctx = ctx.clone();
        {
            let mut state = state.lock().unwrap();
            state.opening = true;
            state.opened = None;
        }
        std::thread::spawn(move || {
            let result = opener(&request);
            let mut state = state.lock().unwrap();
            state.opening = false;
            state.opened = result.transpose();
            drop(state);
            ctx.request_repaint();
        });
    }

    pub(super) fn open_in_button(
        &mut self,
        ui: &mut egui::Ui,
        path: &std::path::Path,
        part: Part,
        enabled: bool,
    ) {
        let mut picked = None;
        let label = format!(
            "Open in {}",
            sessions::open_in_label(self.open_in).to_lowercase()
        );
        ui.add_enabled_ui(enabled, |ui| {
            let split = widgets::split_button(ui, Button::tonal(label), "Open in…", |ui| {
                ui.set_min_width(widgets::popup_width(ui.ctx(), 180.0));
                for choice in [OpenIn::WordProcessor, OpenIn::TextEditor] {
                    if ui.button(sessions::open_in_label(choice)).clicked() {
                        picked = Some(choice);
                        ui.close();
                    }
                }
            });
            if split.primary.clicked() {
                picked = Some(self.open_in);
            }
        });
        if let Some(choice) = picked {
            self.open_with(ui.ctx(), path.to_path_buf(), part, choice);
        }
    }

    pub(super) fn open_notice(&self, ui: &mut egui::Ui, state: &SessionsState) {
        if state.opening {
            ui.add_space(12.0);
            a11y::live(&widgets::small(ui, "Opening file…"));
            return;
        }
        let Some(result) = &state.opened else {
            return;
        };
        ui.add_space(12.0);
        let dismissed = match result {
            Ok(opened) => {
                let path = &opened.exported.path;
                let text = if opened.exported.state == "edited" {
                    format!(
                        "Opened existing file {} without overwriting it because it differs from \
                         the current session. Use Export… to write a fresh copy.",
                        path.display()
                    )
                } else {
                    format!("Opened {}.", path.display())
                };
                widgets::banner(ui, Tone::Info, &text, |ui| {
                    let dismissed = ui.add(Button::ghost("Dismiss").small()).clicked();
                    if ui
                        .add(Button::secondary("Show in folder").small())
                        .clicked()
                    {
                        sessions::show_in_folder(path);
                    }
                    dismissed
                })
            }
            Err(why) => widgets::notice(ui, Tone::Error, &format!("Could not open file: {why}")),
        };
        if dismissed {
            self.state.lock().unwrap().opened = None;
        }
    }
}
