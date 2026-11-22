use super::*;

impl SettingsWindow {
    // Present until its status says otherwise, as setup leaves it.
    pub(super) fn whisper_present(&self) -> bool {
        let state = self.whisper_model.lock().unwrap();
        !matches!(&state.status, Some(Ok(status)) if !status.present)
    }

    // (pinned by `window::tests::deleting_the_canary_in_use_goes_back_to_whisper`,
    // `window::tests::deleting_canary_with_no_whisper_export_says_everything_stays_off`)
    pub(super) fn forget_canary(&mut self) -> bool {
        if self.speech.model != SpeechModel::Canary {
            return false;
        }
        self.speech = Speech {
            model: SpeechModel::Whisper,
            ..self.speech
        }
        .normalised();
        self.speech_note = Some(if self.whisper_present() {
            "Canary was deleted. Switched the active speech model back to Whisper.".into()
        } else {
            "Canary was deleted and Whisper is not installed, so captions and dictation are \
             disabled. Run vinowhisper-setup to export a model."
                .into()
        });
        true
    }

    // Whether the speech model changed (pinned by
    // `window::tests::a_canary_delete_that_failed_leaves_canary_the_speech_model`).
    pub(super) fn settle_canary_delete(&mut self, state: &ModelState) -> bool {
        if !self.canary_delete_pending || state.busy {
            return false;
        }
        self.canary_delete_pending = false;
        matches!(&state.status, Some(Ok(status)) if !status.present) && self.forget_canary()
    }

    pub(super) fn settle_whisper_delete(&mut self, state: &ModelState) {
        if !self.whisper_delete_pending || state.busy {
            return;
        }
        self.whisper_delete_pending = false;
        // (pinned by `window::tests::a_whisper_delete_that_failed_changes_nothing_on_the_page`)
        if matches!(&state.status, Some(Ok(status)) if !status.present) {
            self.forget_whisper();
        }
    }

    // (pinned by `window::tests::forgetting_whisper_in_use_leaves_a_note`)
    pub(super) fn forget_whisper(&mut self) {
        self.multilingual = false;
        if self.speech.model == SpeechModel::Whisper {
            self.speech_note = Some(
                "Whisper was deleted, so captions and dictation are disabled. Run \
                 vinowhisper-setup to export it again."
                    .into(),
            );
        }
    }

    pub(super) fn model_state(&self, tool: Tool) -> Arc<Mutex<ModelState>> {
        match tool {
            Tool::Cleanup => Arc::clone(&self.model),
            Tool::Canary => Arc::clone(&self.canary_model),
            Tool::Whisper => Arc::clone(&self.whisper_model),
        }
    }

    pub(super) fn refresh_model(&self, ctx: &egui::Context, tool: Tool) {
        let model = self.model_state(tool);
        let ctx = ctx.clone();
        std::thread::spawn(move || {
            let status = model::status(tool);
            model.lock().unwrap().status = Some(status);
            ctx.request_repaint();
        });
    }

    pub(super) fn run_model(&self, ctx: &egui::Context, tool: Tool, action: &'static str) {
        let model = self.model_state(tool);
        let ctx = ctx.clone();
        {
            let mut state = model.lock().unwrap();
            state.busy = true;
            state.error.clear();
            if action == "fetch" {
                state.fetching = Some((0, 0));
            }
        }
        std::thread::spawn(move || {
            let result = model::run(tool, action, |line| {
                if let Line::Progress { done, total } = line {
                    model.lock().unwrap().fetching = Some((done, total));
                    ctx.request_repaint();
                }
            });
            let status = model::status(tool);
            let mut state = model.lock().unwrap();
            state.busy = false;
            state.fetching = None;
            state.error = result.err().unwrap_or_default();
            state.status = Some(status);
            drop(state);
            ctx.request_repaint();
        });
    }

    pub(super) fn model_rows(
        &mut self,
        ui: &mut egui::Ui,
        tool: Tool,
        status: &Status,
        state: &ModelState,
    ) {
        let p = theme::palette(ui.ctx());
        let mut confirm = match tool {
            Tool::Cleanup => self.confirm_delete,
            Tool::Canary => self.confirm_delete_canary,
            Tool::Whisper => unreachable!("whisper_rows draws Whisper"),
        };
        let on_disk = model::size(status.bytes + status.cache_bytes);
        widgets::group_label(ui, &status.label);
        widgets::group(ui, |ui| {
            let help = match (tool, confirm) {
                (Tool::Cleanup, false) => "Verified against pinned SHA-256 checksums.".to_owned(),
                (Tool::Cleanup, true) => format!(
                    "Deletes the model and its compiled cache ({on_disk}). Dictation will be typed \
                     without cleanup until you download it again."
                ),
                (_, false) => {
                    "INT8 for NPU, fp16 for CPU and GPU. Verified against pinned SHA-256 \
                     checksums. Licensed under CC-BY-4.0 by NVIDIA."
                        .to_owned()
                }
                (_, true) if self.speech.model == SpeechModel::Canary => {
                    if self.whisper_present() {
                        format!(
                            "Deletes Canary ({on_disk}). Because it is the active speech model, \
                             Whisper will be used instead."
                        )
                    } else {
                        format!(
                            "Deletes Canary ({on_disk}). Because Whisper is not installed, \
                             captions and dictation will remain disabled until you run \
                             vinowhisper-setup."
                        )
                    }
                }
                (_, true) => {
                    format!("Deletes Canary ({on_disk}). You can download it again later.")
                }
            };
            let row = Row::new("Download").help(&help);
            let row = if confirm {
                row.tint(p.error_tint).tone(Tone::Error)
            } else {
                row
            };
            row.show(ui, |ui, _| {
                if confirm {
                    if ui
                        .add(Button::destructive("Delete model").small())
                        .clicked()
                    {
                        confirm = false;
                        if tool == Tool::Canary {
                            self.canary_delete_pending = true;
                        }
                        self.run_model(ui.ctx(), tool, "delete");
                    }
                    if ui.add(Button::ghost("Cancel").small()).clicked() {
                        confirm = false;
                    }
                    return;
                }
                let deletable = status.present && !state.busy;
                if ui
                    .add_enabled(deletable, Button::secondary("Delete…"))
                    .clicked()
                {
                    confirm = true;
                }
                let label = if status.present { "Verify" } else { "Download" };
                let button = if status.present {
                    Button::secondary(label)
                } else {
                    Button::primary(label)
                };
                if ui.add_enabled(!state.busy, button).clicked() {
                    confirm = false;
                    self.run_model(ui.ctx(), tool, "fetch");
                }
                let shown = if let Some((done, total)) = state.fetching {
                    let fraction = done as f32 / total.max(1) as f32;
                    let text = if total == 0 {
                        "Starting…".to_owned()
                    } else {
                        format!("{} of {}", model::size(done), model::size(total))
                    };
                    let shown = widgets::small(ui, text);
                    widgets::progress(ui, fraction, 120.0);
                    shown
                } else if state.busy {
                    widgets::chip(ui, ChipKind::Neutral, "Deleting…")
                } else if status.present {
                    widgets::chip(
                        ui,
                        ChipKind::Ok,
                        &format!("Downloaded, {}", model::size(status.bytes)),
                    )
                } else {
                    widgets::chip(
                        ui,
                        ChipKind::Warn,
                        &format!("Not downloaded ({})", model::size(status.download_bytes)),
                    )
                };
                a11y::live(&shown);
            });
            if !state.error.is_empty() {
                Row::new("Last attempt")
                    .help(&format!("The last attempt failed: {}", state.error))
                    .tone(Tone::Error)
                    .show(ui, |_, _| {});
            }
            if tool != Tool::Cleanup {
                return;
            }
            Row::new("Compiled cache")
                .help("Generated on first load and removed when you delete the model.")
                .show(ui, |ui, _| {
                    ui.label(model::size(status.cache_bytes));
                });
        });
        match tool {
            Tool::Cleanup => self.confirm_delete = confirm,
            Tool::Canary => self.confirm_delete_canary = confirm,
            Tool::Whisper => {}
        }
    }

    // No fetch: vinowhisper-setup exports Whisper.
    pub(super) fn whisper_rows(&mut self, ui: &mut egui::Ui, status: &Status, state: &ModelState) {
        let p = theme::palette(ui.ctx());
        let in_use = self.speech.model == SpeechModel::Whisper;
        let all = model::size(status.bytes + status.source_bytes);
        widgets::group_label(ui, &status.label);
        widgets::group(ui, |ui| {
            let confirm = self.confirm_delete_whisper;
            let help = match (confirm, in_use) {
                (false, _) => {
                    "Exported for this device by vinowhisper-setup for captions and dictation."
                        .to_owned()
                }
                (true, true) => format!(
                    "Deletes all Whisper exports and the source download ({all}). Captions and \
                     dictation will stop until you run vinowhisper-setup again."
                ),
                (true, false) => format!(
                    "Deletes all Whisper exports and the source download ({all}). Canary will \
                     continue running. Run vinowhisper-setup to use Whisper again."
                ),
            };
            let row = Row::new("Exported model").help(&help);
            let row = if confirm {
                row.tint(p.error_tint).tone(Tone::Error)
            } else {
                row
            };
            row.show(ui, |ui, _| {
                if confirm {
                    if ui
                        .add(Button::destructive("Delete Whisper").small())
                        .clicked()
                    {
                        self.confirm_delete_whisper = false;
                        self.confirm_delete_source = false;
                        self.whisper_delete_pending = true;
                        self.run_model(ui.ctx(), Tool::Whisper, "delete");
                    }
                    if ui.add(Button::ghost("Cancel").small()).clicked() {
                        self.confirm_delete_whisper = false;
                    }
                    return;
                }
                let deletable = (status.present || status.source_bytes > 0) && !state.busy;
                if ui
                    .add_enabled(deletable, Button::secondary("Delete…"))
                    .clicked()
                {
                    self.confirm_delete_whisper = true;
                    self.confirm_delete_source = false;
                }
                let shown = if state.busy {
                    widgets::chip(ui, ChipKind::Neutral, "Deleting…")
                } else if status.present {
                    widgets::chip(
                        ui,
                        ChipKind::Ok,
                        &format!("On disk, {}", model::size(status.bytes)),
                    )
                } else {
                    widgets::chip(ui, ChipKind::Warn, "Not exported")
                };
                a11y::live(&shown);
            });
            if status.source_bytes > 0 {
                let confirm = self.confirm_delete_source;
                let size = model::size(status.source_bytes);
                let help = if confirm {
                    format!(
                        "Deletes the {size} source download. Captions and dictation will continue \
                         working; future exports will re-download it if needed."
                    )
                } else {
                    "Original model files used to build the export. Safe to delete once exported."
                        .to_owned()
                };
                let row = Row::new("Source download").help(&help);
                let row = if confirm {
                    row.tint(p.error_tint).tone(Tone::Error)
                } else {
                    row
                };
                row.show(ui, |ui, _| {
                    if confirm {
                        if ui
                            .add(Button::destructive("Delete source").small())
                            .clicked()
                        {
                            self.confirm_delete_source = false;
                            self.run_model(ui.ctx(), Tool::Whisper, "delete-source");
                        }
                        if ui.add(Button::ghost("Cancel").small()).clicked() {
                            self.confirm_delete_source = false;
                        }
                        return;
                    }
                    if ui
                        .add_enabled(!state.busy, Button::secondary("Delete…"))
                        .clicked()
                    {
                        self.confirm_delete_source = true;
                        self.confirm_delete_whisper = false;
                    }
                    ui.label(size);
                });
            }
            if !state.error.is_empty() {
                Row::new("Last attempt")
                    .help(&format!("The last attempt failed: {}", state.error))
                    .tone(Tone::Error)
                    .show(ui, |_, _| {});
            }
        });
    }
}
