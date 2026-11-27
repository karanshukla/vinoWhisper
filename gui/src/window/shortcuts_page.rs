use super::*;

// The portal takes a moment to answer a fresh tray.
const FRESH_TRAY_PORTAL_DELAY: Duration = Duration::from_millis(1500);

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
                Row::new("Checking shortcuts…").show(ui, |_, _| {});
            }
            Shortcuts::Bound {
                captions,
                dictate,
                configurable,
            } => {
                Row::new("Captions")
                    .help("Toggle floating captions.")
                    .show(ui, |ui, _| keys(ui, &captions));
                Row::new("Dictation")
                    .help("Hold to talk, or tap for hands-free.")
                    .show(ui, |ui, _| keys(ui, &dictate));
                if configurable {
                    Row::new("Edit shortcuts")
                        .help("Opens desktop shortcut settings.")
                        .show(ui, |ui, _| {
                            if ui.add(Button::tonal("Change…")).clicked() {
                                forward::run_tray(&["configure-shortcut"]);
                            }
                        });
                } else {
                    Row::new("Edit shortcuts")
                        .help("Change these under vinoWhisper in your desktop shortcut settings.")
                        .show(ui, |_, _| {});
                }
            }
            Shortcuts::Stopped => {
                Row::new("Tray not running")
                    .help("Start vinoWhisper to use global shortcuts.")
                    .tone(Tone::Warn)
                    .show(ui, |ui, _| {
                        if ui.add(Button::primary("Start vinoWhisper")).clicked() {
                            forward::run_tray(&["--hidden"]);
                            self.ask_shortcuts(ui.ctx(), FRESH_TRAY_PORTAL_DELAY);
                        }
                    });
            }
            Shortcuts::Unavailable => {
                Row::new("Bind manually")
                    .help("Bind these commands in your compositor or keyboard settings.")
                    .show(ui, |_, _| {});
                Row::new("Captions").show(ui, |ui, _| {
                    widgets::mono(ui, "vinowhisper-tray toggle");
                });
                Row::new("Dictation")
                    .help(
                        "Press and release for hold-to-talk, or bind vinowhisper-tray dictate to \
                         toggle.",
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
