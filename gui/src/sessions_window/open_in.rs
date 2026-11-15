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

    // A split button: the last app picked, and a menu for the other.
    pub(super) fn open_in_button(
        &mut self,
        ui: &mut egui::Ui,
        path: &std::path::Path,
        part: Part,
        enabled: bool,
    ) {
        ui.add_enabled_ui(enabled, |ui| {
            ui.spacing_mut().item_spacing.x = 2.0;
            let menu =
                widgets::icon_button(ui, ButtonKind::Secondary, Icon::ChevronDown, "Open in…");
            let mut picked = None;
            egui::Popup::menu(&menu).show(|ui| {
                ui.set_min_width(180.0);
                for choice in [OpenIn::WordProcessor, OpenIn::TextEditor] {
                    if ui.button(sessions::open_in_label(choice)).clicked() {
                        picked = Some(choice);
                        ui.close();
                    }
                }
            });
            let label = format!(
                "Open in {}",
                sessions::open_in_label(self.open_in).to_lowercase()
            );
            if ui.add(Button::secondary(label)).clicked() {
                picked = Some(self.open_in);
            }
            if let Some(choice) = picked {
                self.open_with(ui.ctx(), path.to_path_buf(), part, choice);
            }
        });
    }

    pub(super) fn open_notice(&self, ui: &mut egui::Ui, state: &SessionsState) {
        if state.opening {
            ui.add_space(12.0);
            a11y::live(&widgets::small(ui, "Waiting for the app to open."));
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
                        "Opened {} as it is: it differs from the session (edited, or summarised \
                         again), so it wasn't replaced. Export… writes a fresh copy.",
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
            Err(why) => widgets::notice(ui, Tone::Error, &format!("Couldn't open it: {why}")),
        };
        if dismissed {
            self.state.lock().unwrap().opened = None;
        }
    }
}
