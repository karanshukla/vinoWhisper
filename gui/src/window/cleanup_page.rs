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
            "Beta feature tuned on a single microphone. It may miss quiet words or fall back to \
             the raw transcript if the cleaned output fails validation.",
            |_| {},
        );
        widgets::columns(ui, |ui, column| match column {
            Column::Left => self.cleanup_rows(ui, known.as_ref()),
            Column::Right => self.cleanup_model(ui, &state),
        });
    }

    fn cleanup_rows(&mut self, ui: &mut egui::Ui, known: Option<&Status>) {
        widgets::group_label(ui, "Dictation cleanup");
        widgets::group(ui, |ui| {
            let missing = self.cleanup.enabled && known.is_some_and(|s| !s.present);
            let (help, tone) = if missing {
                (
                    "Download the model below first. Until then, raw transcripts are typed without \
                     cleanup.",
                    Tone::Warn,
                )
            } else {
                (
                    "Locally removes filler words and applies spoken edits like “scratch that”. \
                     Only deletes words; never inserts or rewords text.",
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
                    .help(
                        "Takes about 2 seconds per dictation. Smart skips transcripts with no \
                         fillers or spoken edits.",
                    )
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
                    .help(
                        "Falls back to the raw transcript if exceeded. The first run also includes \
                         model loading time.",
                    )
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
    }

    fn cleanup_model(&mut self, ui: &mut egui::Ui, state: &ModelState) {
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
                self.model_rows(ui, Tool::Cleanup, status, state);
            }
        }
    }
}
