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
        let model_ready = *self.model_ready.lock().unwrap();
        widgets::group_label(ui, "Summaries");
        widgets::group(ui, |ui| {
            let (help, tone) = if model_ready == Some(false) {
                (
                    "The model isn't downloaded yet; get it on the Cleanup page.",
                    Tone::Warn,
                )
            } else {
                (
                    "After each saved session, a small model on this machine writes a short \
                     summary with time stamps. Nothing leaves the machine.",
                    Tone::Neutral,
                )
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
        widgets::group_label(ui, "Saved sessions");
        widgets::group(ui, |ui| {
            let (help, tone) = if self.settings.save_transcripts {
                (
                    "Transcripts and summaries open in a window of their own.",
                    Tone::Neutral,
                )
            } else {
                (
                    "Saving transcripts is off (Captions page), so new sessions aren't kept.",
                    Tone::Warn,
                )
            };
            Row::new("Sessions window")
                .help(help)
                .tone(tone)
                .show(ui, |ui, _| {
                    if ui.add(Button::secondary("Open saved sessions")).clicked() {
                        forward::open_sessions();
                    }
                });
            let (help, tone) = match &self.app_menu_error {
                Some(err) => (format!("Could not change the app menu: {err}"), Tone::Error),
                None => (
                    "Adds vinoWhisper Sessions to the app list, to open saved sessions \
                     without opening Settings first."
                        .to_owned(),
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
        // with_file_name, not the exe itself: after an upgrade that reads "<path> (deleted)".
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

// Plasma and GNOME watch the directory anyway; the cache is for the rest. Off the UI thread.
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
