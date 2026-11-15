use super::*;

impl SettingsWindow {
    pub(super) fn speech(&mut self, ui: &mut egui::Ui) -> bool {
        let mut changed = false;
        if let Some(notice) = self.notice.clone() {
            ui.add_space(12.0);
            if widgets::notice(ui, Tone::Error, &notice) {
                self.notice = None;
            }
        }
        if let Some(note) = self.speech_note.clone() {
            ui.add_space(12.0);
            if widgets::notice(ui, Tone::Info, &note) {
                self.speech_note = None;
            }
        }
        widgets::group_label(ui, "Recognition");
        let canary_state = self.canary_model.lock().unwrap().clone();
        changed |= self.settle_canary_delete(&canary_state);
        let model = self.speech.model;
        // The download's own status once known; until then, what is on disk.
        let canary = match &canary_state.status {
            Some(Ok(status)) => status.present,
            _ => self.canary,
        };
        let whisper_state = self.whisper_model.lock().unwrap().clone();
        self.settle_whisper_delete(&whisper_state);
        let whisper = self.whisper_present();
        let installed = |option: SpeechModel| match option {
            SpeechModel::Whisper => whisper,
            SpeechModel::Canary => canary,
        };
        let multilingual = match model {
            SpeechModel::Whisper => self.multilingual,
            SpeechModel::Canary => true,
        };
        widgets::group(ui, |ui| {
            Row::new("Speech model")
                .help(model_help(model, installed(model)))
                .tone(if installed(model) {
                    Tone::Neutral
                } else {
                    Tone::Warn
                })
                .show(ui, |ui, named| {
                    let before = self.speech.model;
                    let shown = widgets::combo(
                        ui,
                        "speech-model",
                        model_label(before).to_owned(),
                        COMBO_WIDTH,
                    )
                    .show_ui(ui, |ui| {
                        for option in SpeechModel::ALL {
                            let installed = installed(option);
                            let label = if installed {
                                model_label(option).to_owned()
                            } else {
                                format!("{} (not installed)", model_label(option))
                            };
                            ui.add_enabled_ui(installed, |ui| {
                                ui.selectable_value(&mut self.speech.model, option, label);
                            });
                        }
                    });
                    named(shown.response);
                    if self.speech.model != before {
                        let wanted = self.speech;
                        self.speech = self.speech.normalised();
                        self.speech_note = (self.speech.language != wanted.language)
                            .then(|| model_switch_note(wanted));
                        changed = true;
                    }
                });
            Row::new("Language")
                .help(match model {
                    SpeechModel::Whisper if multilingual => {
                        "Detect automatically picks per sentence. Switching reloads the model."
                    }
                    SpeechModel::Whisper => {
                        "Only English is installed. Run vinowhisper-setup --language auto to add \
                         the others."
                    }
                    SpeechModel::Canary => {
                        "Detect automatically translates whatever is spoken into English. To \
                         caption in another language, name it. Switching reloads the model."
                    }
                })
                .show(ui, |ui, named| {
                    ui.add_enabled_ui(multilingual, |ui| {
                        if combo(
                            ui,
                            "language",
                            named,
                            &mut self.speech.language,
                            self.speech.model.languages(),
                            language_label,
                        ) {
                            self.speech = self.speech.normalised();
                            changed = true;
                        }
                    });
                });
            let foreign = multilingual && self.speech.language != Language::En;
            // Canary's auto only ever translates (it has no detection to transcribe with).
            let locked = model == SpeechModel::Canary && self.speech.language == Language::Auto;
            Row::new("Translation")
                .help(if locked {
                    "Always on with Detect automatically: Canary translates whatever is spoken."
                } else if foreign {
                    "Captions arrive in English whatever was spoken."
                } else if self.speech.language == Language::En {
                    "English speech is captioned as it is. To translate, set Language to the \
                     one being spoken, or to Detect automatically."
                } else {
                    "Needs the other languages installed, as above."
                })
                .show(ui, |ui, _| {
                    ui.add_enabled_ui(foreign && !locked, |ui| {
                        let mut translate = self.speech.task == Task::Translate;
                        if widgets::switch(ui, &mut translate, "Translate to English").changed() {
                            self.speech.task = if translate {
                                Task::Translate
                            } else {
                                Task::Transcribe
                            };
                            changed = true;
                        }
                    });
                });
        });
        match &whisper_state.status {
            Some(Ok(status)) => self.whisper_rows(ui, status, &whisper_state),
            Some(Err(why)) => {
                widgets::group_label(ui, "Whisper small");
                widgets::group(ui, |ui| {
                    Row::new("Exported model")
                        .help(&format!(
                            "Could not check what is on disk: {why}. vinowhisper-setup links \
                             the command this page asks."
                        ))
                        .tone(Tone::Error)
                        .show(ui, |_, _| {});
                });
            }
            None => {}
        }
        if let Some(Ok(status)) = &canary_state.status {
            self.model_rows(ui, Tool::Canary, status, &canary_state);
        }
        widgets::group_label(ui, "Dictation");
        widgets::group(ui, |ui| {
            Row::new("Pause media")
                .help(
                    "Pauses music and video that is playing while you dictate, and resumes it \
                     after. Players that ignore the desktop's media controls keep playing.",
                )
                .show(ui, |ui, _| {
                    self.settings_dirty |=
                        widgets::switch(ui, &mut self.settings.pause_media, "Pause media")
                            .changed();
                });
            Row::new("Space between dictations")
                .help(
                    "Types a space before a dictation that starts within 30 seconds of the \
                     last one. A guess: if you changed field in between, the space goes there.",
                )
                .show(ui, |ui, _| {
                    self.settings_dirty |= widgets::switch(
                        ui,
                        &mut self.settings.smart_spacing,
                        "Space between dictations",
                    )
                    .changed();
                });
            Row::new("Keep typing access")
                .help(
                    "Dictation types through the desktop's remote control, which announces \
                     each time it starts. It stays on this long after a dictation. 0 keeps \
                     it on.",
                )
                .show(ui, |ui, named| {
                    self.settings_dirty |= number(
                        ui,
                        named,
                        &mut self.settings.typing_idle_minutes,
                        0..=1440,
                        "min",
                    );
                });
            self.vocabulary_rows(ui);
        });
        changed
    }

    pub(super) fn vocabulary_rows(&mut self, ui: &mut egui::Ui) {
        let full = self.vocabulary.len() >= vocabulary::MAX_TERMS;
        let help = match &self.vocabulary_error {
            Some(error) => error.clone(),
            None => "Names and jargon to expect when you dictate. A near-miss spelling is \
                     corrected to the term as written here; Whisper on a GPU or CPU is also \
                     told to listen for them."
                .to_owned(),
        };
        let tone = if self.vocabulary_error.is_some() {
            Tone::Error
        } else {
            Tone::Neutral
        };
        let mut add = false;
        Row::new("Vocabulary")
            .help(&help)
            .tone(tone)
            .show(ui, |ui, _| {
                let enabled = !full && !self.new_term.trim().is_empty();
                add |= ui.add_enabled(enabled, Button::secondary("Add")).clicked();
                let field = ui.add_enabled(
                    !full,
                    widgets::field(&mut self.new_term, "A name or term", 180.0),
                );
                field.widget_info(|| {
                    egui::WidgetInfo::labeled(egui::Role::TextInput, !full, "New term")
                });
                add |= field.lost_focus() && ui.input(|input| input.key_pressed(egui::Key::Enter));
            });
        if add {
            let added = vocabulary::add_to(&self.vocabulary_path, &self.new_term);
            self.after_vocabulary_change(added.map(|_| ()));
            self.new_term.clear();
        }
        let mut remove = None;
        for term in &self.vocabulary {
            Row::new(term).show(ui, |ui, _| {
                let button = ui.add(Button::ghost("Remove").small());
                button.widget_info(|| {
                    egui::WidgetInfo::labeled(egui::Role::Button, true, format!("Remove {term}"))
                });
                if button.clicked() {
                    remove = Some(term.clone());
                }
            });
        }
        if let Some(term) = remove {
            let removed = vocabulary::remove_from(&self.vocabulary_path, &term);
            self.after_vocabulary_change(removed);
        }
    }

    pub(super) fn after_vocabulary_change(&mut self, result: std::io::Result<()>) {
        self.vocabulary_error = result
            .err()
            .map(|err| format!("Could not write {}: {err}", self.vocabulary_path.display()));
        self.vocabulary = vocabulary::load_from(&self.vocabulary_path);
    }
}
