use super::*;

impl SettingsWindow {
    pub(super) fn speech(&mut self, ui: &mut egui::Ui) -> bool {
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
        let [recognition, _] = widgets::columns(ui, |ui, column| match column {
            Column::Left => self.recognition(ui),
            Column::Right => {
                self.dictation(ui);
                false
            }
        });
        recognition
    }

    fn recognition(&mut self, ui: &mut egui::Ui) -> bool {
        let mut changed = false;
        widgets::group_label(ui, "Recognition");
        let canary_state = self.canary_model.lock().unwrap().clone();
        changed |= self.settle_canary_delete(&canary_state);
        let model = self.speech.model;
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
                        "Detect automatically identifies the language on each window. Changing the \
                         language reloads the model."
                    }
                    SpeechModel::Whisper => {
                        "Only English is installed. Run vinowhisper-setup --language auto to \
                         install multilingual support."
                    }
                    SpeechModel::Canary => {
                        "Detect automatically translates spoken audio into English. Select a \
                         specific language to caption in that language. Switching reloads the \
                         model."
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
            // (pinned by `window::tests::canary_offers_its_own_languages_and_detection_to_translate`)
            let locked = model == SpeechModel::Canary && self.speech.language == Language::Auto;
            Row::new("Translation")
                .help(if locked {
                    "Always enabled when Detect automatically is selected with Canary."
                } else if foreign {
                    "Translates spoken audio into English captions."
                } else if self.speech.language == Language::En {
                    "English audio is transcribed directly. To translate another language into \
                     English, select that language or Detect automatically."
                } else {
                    "Requires multilingual support (see Language above)."
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
                            "Could not check installed models: {why}. Run vinowhisper-setup to \
                             link the required command."
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
        changed
    }

    fn dictation(&mut self, ui: &mut egui::Ui) {
        widgets::group_label(ui, "Dictation");
        widgets::group(ui, |ui| {
            Row::new("Pause media")
                .help(
                    "Pauses active media playback while dictating and resumes it afterward. \
                     Players without MPRIS support are unaffected.",
                )
                .show(ui, |ui, _| {
                    self.settings_dirty |=
                        widgets::switch(ui, &mut self.settings.pause_media, "Pause media")
                            .changed();
                });
            Row::new("Space between dictations")
                .help(
                    "Inserts a leading space when starting a new dictation within 30 seconds of \
                     the previous one. Applies even if you switched text fields.",
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
                    "Dictation uses the desktop remote-control portal, which shows a notification \
                     when a session starts. Keeps the session open for this duration after \
                     dictation (0 keeps it open).",
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
    }

    pub(super) fn vocabulary_rows(&mut self, ui: &mut egui::Ui) {
        let full = self.vocabulary.len() >= vocabulary::MAX_TERMS;
        let help = match &self.vocabulary_error {
            Some(error) => error.clone(),
            None => "Custom names and terms for dictation. Close misspellings are corrected \
                     automatically, and Whisper uses them as decoding hints when supported."
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
                    widgets::field(&mut self.new_term, "Enter a name or term", 180.0),
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
