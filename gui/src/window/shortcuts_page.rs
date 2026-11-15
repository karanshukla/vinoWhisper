use super::*;

impl SettingsWindow {
    pub(super) fn ask_shortcuts(&self, ctx: &egui::Context, after: Duration) {
        let shortcuts = Arc::clone(&self.shortcuts);
        let ctx = ctx.clone();
        std::thread::spawn(move || {
            std::thread::sleep(after);
            let answer = forward::query_shortcuts();
            *shortcuts.lock().unwrap() = answer;
            ctx.request_repaint();
        });
    }

    pub(super) fn shortcuts(&mut self, ui: &mut egui::Ui) {
        let state = self.shortcuts.lock().unwrap().clone();
        widgets::group_label(ui, "Global shortcuts");
        widgets::group(ui, |ui| match state {
            Shortcuts::Asking | Shortcuts::Pending => {
                Row::new("Asking the desktop which keys are bound.").show(ui, |_, _| {});
            }
            Shortcuts::Bound {
                captions,
                dictate,
                configurable,
            } => {
                Row::new("Captions")
                    .help("Shows or hides the floating captions.")
                    .show(ui, |ui, _| keys(ui, &captions));
                Row::new("Dictation")
                    .help("Hold to talk and release to type, or tap to start and tap to finish.")
                    .show(ui, |ui, _| keys(ui, &dictate));
                if configurable {
                    Row::new("Change them")
                        .help("Opens your desktop's shortcut settings.")
                        .show(ui, |ui, _| {
                            if ui.add(Button::secondary("Change…")).clicked() {
                                forward::run_tray(&["configure-shortcut"]);
                            }
                        });
                } else {
                    Row::new("Change them")
                        .help(
                            "In your desktop's shortcut settings, where they are listed under \
                             vinoWhisper.",
                        )
                        .show(ui, |_, _| {});
                }
            }
            Shortcuts::Stopped => {
                Row::new("Not running")
                    .help("vinoWhisper is not running, so no shortcut is active.")
                    .tone(Tone::Warn)
                    .show(ui, |ui, _| {
                        if ui.add(Button::primary("Start vinoWhisper")).clicked() {
                            forward::run_tray(&["--hidden"]);
                            // The portal takes a moment to answer a fresh tray.
                            self.ask_shortcuts(ui.ctx(), Duration::from_millis(1500));
                        }
                    });
            }
            Shortcuts::Unavailable => {
                Row::new("No shortcuts portal")
                    .help(
                        "This desktop has none. Bind these commands to keys in its keyboard \
                         settings instead.",
                    )
                    .show(ui, |_, _| {});
                Row::new("Captions").show(ui, |ui, _| {
                    widgets::mono(ui, "vinowhisper-tray toggle");
                });
                Row::new("Dictation")
                    .help(
                        "On the key's press and release, for hold to talk. Or vinowhisper-tray \
                         dictate on one key, to tap.",
                    )
                    .show(ui, |ui, _| {
                        ui.with_layout(Layout::top_down(Align::Max), |ui| {
                            widgets::mono(ui, "vinowhisper-tray dictate-press");
                            widgets::mono(ui, "vinowhisper-tray dictate-release");
                        });
                    });
            }
            Shortcuts::Unknown(reason) => {
                Row::new("Unknown").help(&reason).show(ui, |_, _| {});
            }
        });
    }
}

pub(super) fn keys(ui: &mut egui::Ui, trigger: &str) {
    if trigger.is_empty() {
        widgets::muted(ui, "Not bound");
        return;
    }
    widgets::keys(ui, trigger);
}
