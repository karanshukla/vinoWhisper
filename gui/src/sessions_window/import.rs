use super::*;

impl SessionsWindow {
    pub(super) fn import(&self, ctx: &egui::Context) {
        let state = Arc::clone(&self.state);
        {
            let mut state = state.lock().unwrap();
            if state.import.is_some() {
                return;
            }
            state.import = Some(Import::default());
            state.imported = None;
            state.import_warnings.clear();
        }
        let importer = Arc::clone(&self.importer);
        let ctx = ctx.clone();
        std::thread::spawn(move || {
            let result = importer(&mut |event| {
                let mut locked = state.lock().unwrap();
                if let Some(import) = locked.import.as_mut() {
                    match event {
                        ImportEvent::Chosen(name) => import.name = name,
                        ImportEvent::Started { pid } => import.pid = Some(pid),
                        ImportEvent::OutputEnded => import.pid = None,
                        ImportEvent::Progress {
                            position_s,
                            duration_s,
                        } => {
                            import.position_s = position_s;
                            import.duration_s = duration_s;
                        }
                        ImportEvent::Warning(what) => import.warnings.push(what),
                        ImportEvent::Done(_) | ImportEvent::Failed(_) => {}
                    }
                }
                drop(locked);
                ctx.request_repaint();
            });
            let mut locked = state.lock().unwrap();
            let import = locked.import.take().unwrap_or_default();
            let cancelled = import.cancelled;
            locked.import_warnings = import.warnings;
            locked.imported = match result {
                Ok(Some(path)) => Some(Ok(path)),
                // (pinned by `sessions_window::tests::a_failed_import_says_why_and_a_closed_dialog_says_nothing`,
                // `sessions_window::tests::cancel_marks_the_import_and_its_ending_is_not_reported_as_a_failure`)
                Ok(None) => None,
                Err(_) if cancelled => None,
                Err(why) => Some(Err(why)),
            };
            locked.relist = matches!(locked.imported, Some(Ok(_)));
            drop(locked);
            ctx.request_repaint();
        });
    }

    pub(super) fn cancel_import(&self) {
        let mut state = self.state.lock().unwrap();
        if let Some(import) = state.import.as_mut() {
            import.cancelled = true;
            if let Some(pid) = import.pid {
                sessions::cancel_import(pid);
            }
        }
    }

    pub(super) fn import_notice(&self, ui: &mut egui::Ui, state: &SessionsState) {
        if let Some(import) = &state.import {
            ui.add_space(12.0);
            if import.name.is_empty() {
                a11y::live(&widgets::small(ui, "Waiting for file dialog…"));
                return;
            }
            let text = if import.cancelled {
                format!("Stopping {}.", import.name)
            } else {
                match import.duration_s {
                    Some(total) => format!(
                        "Transcribing {}: {} of {}.",
                        import.name,
                        sessions::stamp(import.position_s as u64),
                        sessions::stamp(total as u64)
                    ),
                    None => format!(
                        "Transcribing {}: {} elapsed.",
                        import.name,
                        sessions::stamp(import.position_s as u64)
                    ),
                }
            };
            let cancel = widgets::banner(ui, Tone::Info, &text, |ui| {
                let cancel = ui
                    .add_enabled(!import.cancelled, Button::ghost("Cancel").small())
                    .clicked();
                let roomy = ui.ctx().content_rect().width() >= NARROW_BELOW;
                if let Some(total) = import.duration_s.filter(|total| roomy && *total > 0.0) {
                    widgets::progress(ui, (import.position_s / total) as f32, 96.0);
                }
                cancel
            });
            if cancel {
                self.cancel_import();
            }
            return;
        }
        let Some(result) = &state.imported else {
            return;
        };
        ui.add_space(12.0);
        let dismissed = match result {
            Ok(path) => widgets::banner(
                ui,
                Tone::Info,
                "Import complete. The session has been added to the list and queued for \
                 summarisation.",
                |ui| {
                    let dismissed = ui.add(Button::ghost("Dismiss").small()).clicked();
                    let read = ui.add(Button::secondary("Transcript").small());
                    if a11y::for_what(read, "of the import").clicked() {
                        self.open(
                            ui.ctx(),
                            Target {
                                path: path.clone(),
                                part: Part::Transcript,
                                at_s: None,
                            },
                        );
                    }
                    dismissed
                },
            ),
            Err(why) => widgets::notice(ui, Tone::Error, &format!("Import failed: {why}")),
        };
        let mut warned = false;
        for warning in &state.import_warnings {
            ui.add_space(8.0);
            warned |= widgets::notice(ui, Tone::Warn, &format!("Import note: {warning}."));
        }
        if dismissed || warned {
            let mut state = self.state.lock().unwrap();
            if dismissed {
                state.imported = None;
            }
            state.import_warnings.clear();
        }
    }
}
