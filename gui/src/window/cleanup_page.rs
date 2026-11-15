use super::*;

impl SettingsWindow {
    pub(super) fn cleanup(&mut self, ui: &mut egui::Ui) {
        let state = self.model.lock().unwrap().clone();
        let known = match &state.status {
            Some(Ok(status)) => Some(status.clone()),
            _ => None,
        };
        ui.add_space(12.0);
        widgets::banner(
            ui,
            Tone::Warn,
            "New and tuned on one microphone. It may miss a quiet word, or leave text as you \
             said it when the model's answer does not check out.",
            |_| {},
        );
        widgets::group_label(ui, "Dictation cleanup");
        widgets::group(ui, |ui| {
            let missing = self.cleanup.enabled && known.as_ref().is_some_and(|s| !s.present);
            let (help, tone) = if missing {
                (
                    "Download the model below first; until then the raw text is typed.",
                    Tone::Warn,
                )
            } else {
                (
                    "Drops ums and acts on “scratch that”, on this machine. It only ever \
                     deletes words.",
                    Tone::Neutral,
                )
            };
            Row::new("Clean up dictation")
                .help(help)
                .tone(tone)
                .show(ui, |ui, _| {
                    self.cleanup_dirty |=
                        widgets::switch(ui, &mut self.cleanup.enabled, "Clean up dictation")
                            .changed();
                });
            let off = !self.cleanup.enabled;
            ui.add_enabled_ui(!off, |ui| {
                Row::new("Run")
                    .help("About two seconds each. Smart skips text with nothing to fix.")
                    .show(ui, |ui, named| {
                        self.cleanup_dirty |= combo(
                            ui,
                            "cleanup-mode",
                            named,
                            &mut self.cleanup.mode,
                            &CleanupMode::ALL,
                            mode_label,
                        );
                    });
                Row::new("Time limit")
                    .help("Then the raw text is typed. The first run also loads the model.")
                    .show(ui, |ui, named| {
                        widgets::small(ui, "s");
                        let slider = ui.add(
                            egui::DragValue::new(&mut self.cleanup.budget_s)
                                .range(0.5..=30.0)
                                .speed(0.1)
                                .fixed_decimals(1),
                        );
                        self.cleanup_dirty |= named(slider)
                            .on_hover_cursor(egui::CursorIcon::Text)
                            .changed();
                    });
            });
        });

        match &state.status {
            None => {
                widgets::group_label(ui, "Model");
                widgets::group(ui, |ui| {
                    Row::new("Download").show(ui, |ui, _| {
                        a11y::live(&widgets::chip(ui, ChipKind::Neutral, "Checking…"));
                    });
                });
            }
            Some(Err(why)) => {
                widgets::group_label(ui, "Model");
                widgets::group(ui, |ui| {
                    Row::new("Download")
                        .help(why)
                        .tone(Tone::Error)
                        .show(ui, |_, _| {});
                });
            }
            Some(Ok(status)) => {
                self.model_rows(ui, Tool::Cleanup, status, &state);
            }
        }
    }
}
