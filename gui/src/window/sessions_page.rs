use super::*;

impl SettingsWindow {
    pub(super) fn refresh_model_ready(&self, ctx: &egui::Context) {
        let model_ready = Arc::clone(&self.model_ready);
        let ctx = ctx.clone();
        std::thread::spawn(move || {
            let ready = sessions::list().ok().map(|listing| listing.model_ready);
            *model_ready.lock().unwrap() = ready;
            ctx.request_repaint();
        });
    }

    pub(super) fn sessions(&mut self, ui: &mut egui::Ui) {
        widgets::columns(ui, |ui, column| match column {
            Column::Left => self.summaries(ui),
            Column::Right => self.saved_sessions(ui),
        });
    }

    fn summaries(&mut self, ui: &mut egui::Ui) {
        let model_ready = *self.model_ready.lock().unwrap();
        widgets::group_label(ui, "Summaries");
        widgets::group(ui, |ui| {
            let (help, tone) = if model_ready == Some(false) {
                ("Needs the model from Cleanup.", Tone::Warn)
            } else {
                ("Summarise each saved session locally.", Tone::Neutral)
            };
            Row::new("Summarise automatically")
                .help(help)
                .tone(tone)
                .show(ui, |ui, _| {
                    self.summaries_dirty |= widgets::switch(
                        ui,
                        &mut self.summaries.auto,
                        "Summarise saved sessions automatically",
                    )
                    .changed();
                    if model_ready == Some(false)
                        && ui.add(Button::secondary("Go to Cleanup").small()).clicked()
                    {
                        self.page = Page::Cleanup;
                    }
                });
        });
    }

    fn saved_sessions(&mut self, ui: &mut egui::Ui) {
        widgets::group_label(ui, "Saved sessions");
        widgets::group(ui, |ui| {
            let (help, tone) = if self.settings.save_transcripts {
                ("", Tone::Neutral)
            } else {
                ("Transcript saving is off in Captions.", Tone::Warn)
            };
            Row::new("Sessions window")
                .help(help)
                .tone(tone)
                .show(ui, |ui, _| {
                    if ui.add(Button::tonal("Open saved sessions")).clicked() {
                        forward::open_sessions();
                    }
                    if !self.settings.save_transcripts
                        && ui.add(Button::secondary("Turn on").small()).clicked()
                    {
                        self.settings.save_transcripts = true;
                        self.settings_dirty = true;
                    }
                });
            let (help, tone) = match &self.app_menu_error {
                Some(err) => (format!("Couldn't update the app menu: {err}"), Tone::Error),
                None => (
                    "Show vinoWhisper Sessions in your app menu.".to_owned(),
                    Tone::Neutral,
                ),
            };
            Row::new("App menu")
                .help(&help)
                .tone(tone)
                .show(ui, |ui, _| {
                    let mut shown = self.in_app_menu;
                    if widgets::switch(ui, &mut shown, "Show saved sessions in the app menu")
                        .changed()
                    {
                        self.set_app_menu(shown);
                    }
                });
        });
    }

    pub(super) fn set_app_menu(&mut self, show: bool) {
        // Not the exe itself, which reads "<path> (deleted)" after an upgrade.
        let gui = std::env::current_exe()
            .map(|exe| exe.with_file_name("vinowhisper-gui"))
            .unwrap_or_else(|_| PathBuf::from("vinowhisper-gui"));
        match vinowhisper_settings::show_sessions_entry_in(&self.apps_data, show, &gui) {
            Ok(()) => {
                self.in_app_menu = show;
                self.app_menu_error = None;
                refresh_app_menu(self.apps_data.join("applications"));
            }
            Err(err) => self.app_menu_error = Some(err.to_string()),
        }
    }
}

// Plasma and GNOME watch the directory; the cache is for the rest.
pub(super) fn refresh_app_menu(dir: PathBuf) {
    std::thread::spawn(move || {
        let _ = std::process::Command::new("update-desktop-database")
            .arg(dir)
            .stdin(std::process::Stdio::null())
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .status();
    });
}
