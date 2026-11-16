use super::*;

impl SessionsWindow {
    pub(super) fn ask_export(&mut self, path: PathBuf, scope: Scope, has_summary: bool) {
        self.export_dialog = Some(ExportDialog {
            path,
            scope: if has_summary {
                scope
            } else {
                Scope::Transcript
            },
            format: Format::Markdown,
            has_summary,
        });
    }

    pub(super) fn export(&self, ctx: &egui::Context, request: ExportRequest) {
        let state = Arc::clone(&self.state);
        let exporter = Arc::clone(&self.exporter);
        let ctx = ctx.clone();
        {
            let mut state = state.lock().unwrap();
            state.exporting = true;
            state.exported = None;
        }
        std::thread::spawn(move || {
            let result = exporter(&request);
            let mut state = state.lock().unwrap();
            state.exporting = false;
            state.exported = result.transpose();
            drop(state);
            ctx.request_repaint();
        });
    }

    // In place, not a modal: the window may be half a screen.
    pub(super) fn export_panel(&mut self, ui: &mut egui::Ui, path: &std::path::Path) {
        let Some(dialog) = self.export_dialog.as_mut().filter(|d| d.path == path) else {
            return;
        };
        let mut save = false;
        let mut cancel = false;
        widgets::group_label(ui, "Export");
        ui.add_space(6.0);
        widgets::group(ui, |ui| {
            if dialog.has_summary {
                widgets::Row::new("Part").show(ui, |ui, _| {
                    widgets::segmented(
                        ui,
                        "Part",
                        &mut dialog.scope,
                        &[
                            (Scope::Summary, "Summary"),
                            (Scope::Transcript, "Transcript"),
                            (Scope::Both, "Both"),
                        ],
                    );
                });
            } else {
                widgets::Row::new("Part")
                    .help("The transcript: this session has no summary yet.")
                    .show(ui, |_, _| {});
            }
            let help = match dialog.format {
                Format::Markdown => {
                    "For notes apps like Obsidian. The session's details go in its frontmatter."
                }
                Format::Text => "No markup. Opens anywhere.",
                Format::Odt => "For LibreOffice, Word and Google Docs.",
            };
            widgets::Row::new("Format").help(help).show(ui, |ui, _| {
                widgets::segmented(
                    ui,
                    "Format",
                    &mut dialog.format,
                    &[
                        (Format::Markdown, "Markdown"),
                        (Format::Text, "Plain text"),
                        (Format::Odt, "Document (.odt)"),
                    ],
                );
            });
            widgets::Row::new("Where")
                .help("The desktop's save dialog asks next.")
                .show(ui, |ui, _| {
                    save = ui.add(Button::primary("Save…").small()).clicked();
                    cancel = ui.add(Button::ghost("Cancel").small()).clicked();
                });
        });
        if save {
            let request = ExportRequest {
                path: dialog.path.clone(),
                scope: dialog.scope,
                format: dialog.format,
            };
            self.export_dialog = None;
            self.export(ui.ctx(), request);
        } else if cancel {
            self.export_dialog = None;
        }
    }

    pub(super) fn export_notice(&self, ui: &mut egui::Ui, state: &SessionsState) {
        if state.exporting {
            ui.add_space(12.0);
            a11y::live(&widgets::small(ui, "Waiting for the save dialog."));
            return;
        }
        let Some(result) = &state.exported else {
            return;
        };
        ui.add_space(12.0);
        let dismissed = match result {
            Ok(exported) => {
                let mut text = if exported.saved_without_dialog {
                    format!(
                        "There's no save dialog on this desktop, so it's in {}.",
                        exported.path.display()
                    )
                } else {
                    format!("Exported to {}.", exported.path.display())
                };
                if exported.summary_missing {
                    text.push_str(" The transcript only: there's no summary yet.");
                }
                widgets::banner(ui, Tone::Info, &text, |ui| {
                    let dismissed = ui.add(Button::ghost("Dismiss").small()).clicked();
                    if ui
                        .add(Button::secondary("Show in folder").small())
                        .clicked()
                    {
                        sessions::show_in_folder(&exported.path);
                    }
                    dismissed
                })
            }
            Err(why) => widgets::notice(ui, Tone::Error, &format!("Export didn't work: {why}")),
        };
        if dismissed {
            self.state.lock().unwrap().exported = None;
        }
    }
}
