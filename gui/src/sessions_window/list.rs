use super::*;

impl SessionsWindow {
    pub(super) fn list(&mut self, ui: &mut egui::Ui, state: &SessionsState) {
        let p = theme::palette(ui.ctx());
        let listing = match &state.listing {
            Some(Ok(listing)) => Some(listing.clone()),
            _ => None,
        };
        self.export_notice(ui, state);
        self.open_notice(ui, state);
        self.import_notice(ui, state);
        if listing.as_ref().is_some_and(|l| !l.model_ready) {
            ui.add_space(12.0);
            widgets::banner(
                ui,
                Tone::Warn,
                "Download the summary model in Settings (Cleanup).",
                |ui| {
                    if ui.add(Button::secondary("Open settings").small()).clicked() {
                        forward::open_settings("cleanup");
                    }
                },
            );
        }
        if !self.save_transcripts {
            ui.add_space(12.0);
            widgets::banner(
                ui,
                Tone::Info,
                "Transcript saving is off. New sessions won't be saved.",
                |ui| {
                    if ui.add(Button::secondary("Open settings").small()).clicked() {
                        forward::open_settings("captions");
                    }
                },
            );
        }
        if let Some(listing) = listing.as_ref().filter(|l| l.backfill > 0) {
            let count = listing.backfill;
            ui.add_space(12.0);
            let what = format!("{count} transcript{}", if count == 1 { "" } else { "s" });
            let shown = egui::Frame::new()
                .fill(p.surface)
                .corner_radius(egui::CornerRadius {
                    nw: 0,
                    sw: 0,
                    ne: 6,
                    se: 6,
                })
                .inner_margin(egui::Margin {
                    left: 15,
                    right: 12,
                    top: 10,
                    bottom: 10,
                })
                .show(ui, |ui| {
                    ui.set_width(ui.available_width());
                    ui.horizontal(|ui| {
                        ui.spacing_mut().item_spacing.x = 12.0;
                        ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                            let button =
                                ui.add_enabled(!state.busy, Button::tonal("Bundle and summarise"));
                            if a11y::for_what(button, &what).clicked() {
                                self.act(ui.ctx(), "backfill", None);
                            }
                            ui.with_layout(Layout::top_down(Align::Min), |ui| {
                                ui.spacing_mut().item_spacing.y = 2.0;
                                ui.label(
                                    RichText::new(format!("{what} saved as plain text"))
                                        .family(theme::semibold()),
                                );
                                widgets::small(
                                    ui,
                                    "Convert older plain-text transcripts so they can be \
                                     summarised.",
                                );
                            });
                        });
                    });
                });
            let bar = egui::Rect::from_min_size(
                shown.response.rect.min,
                egui::vec2(3.0, shown.response.rect.height()),
            );
            ui.painter().rect_filled(bar, 0.0, p.rule_strong);
        }

        let n = listing.as_ref().map_or(0, |l| l.sessions.len());
        section(ui, "Sessions", n);
        if !state.error.is_empty() {
            widgets::notice(ui, Tone::Error, &format!("Action failed: {}", state.error));
            ui.add_space(8.0);
        }
        match &state.listing {
            None => {
                a11y::live(&widgets::chip(ui, ChipKind::Neutral, "Reading…"));
            }
            Some(Err(why)) => {
                widgets::banner(ui, Tone::Error, why, |_| {});
            }
            Some(Ok(listing)) if listing.sessions.is_empty() => {
                widgets::muted(ui, "No saved sessions yet.");
            }
            Some(Ok(listing)) => {
                ui.scope(|ui| {
                    ui.spacing_mut().item_spacing.y = 0.0;
                    let mut last_rect = None;
                    for entry in &listing.sessions {
                        last_rect = Some(self.card(ui, entry, listing, state.busy));
                        self.export_panel(ui, &entry.path);
                    }
                    if let Some(rect) = last_rect {
                        ui.painter().hline(
                            rect.left()..=rect.right(),
                            rect.bottom(),
                            egui::Stroke::new(1.0_f32, p.rule),
                        );
                    }
                });
            }
        }
    }

    pub(super) fn card(
        &mut self,
        ui: &mut egui::Ui,
        entry: &sessions::Entry,
        listing: &Listing,
        busy: bool,
    ) -> egui::Rect {
        let p = theme::palette(ui.ctx());
        let path = entry.path.clone();
        let deleting = self.confirm_delete.as_ref() == Some(&path);
        let (fill, margin) = if deleting {
            (p.error_tint, egui::Margin::symmetric(12, 12))
        } else {
            (Color32::TRANSPARENT, egui::Margin::symmetric(0, 12))
        };
        let shown = egui::Frame::new()
            .fill(fill)
            .inner_margin(margin)
            .show(ui, |ui| {
                ui.set_width(ui.available_width());
                let working = listing.current.as_ref() == Some(&entry.path);
                let waiting = working || listing.queued.contains(&entry.path);
                let title = entry.title.clone().filter(|_| entry.error.is_none());
                let name = title.clone().unwrap_or_else(|| entry.when());
                let mut facts = Vec::new();
                if title.is_some() {
                    facts.push(entry.when());
                }
                facts.extend(entry.length());
                facts.extend(entry.source.clone());
                facts.extend(entry.playing.clone());
                ui.horizontal(|ui| {
                    ui.spacing_mut().item_spacing.x = 8.0;
                    ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                        if deleting {
                            if ui
                                .add(Button::destructive("Delete session").small())
                                .clicked()
                            {
                                self.confirm_delete = None;
                                self.act(ui.ctx(), "delete", Some(path.clone()));
                            }
                            if ui.add(Button::ghost("Cancel").small()).clicked() {
                                self.confirm_delete = None;
                            }
                        } else {
                            self.card_actions(ui, entry, listing, busy, waiting, working);
                        }
                        ui.with_layout(Layout::top_down(Align::Min), |ui| {
                            ui.spacing_mut().item_spacing.y = 2.0;
                            let shown = ui.add(
                                egui::Label::new(RichText::new(&name).family(theme::semibold()))
                                    .truncate(),
                            );
                            a11y::heading(&shown, &name, 3);
                            session_hover_preview(&shown, entry, &name);
                            if deleting {
                                ui.label(
                                    RichText::new("Delete this session and its summary?")
                                        .size(12.5)
                                        .color(p.error),
                                );
                            } else {
                                widgets::small(ui, facts.join(" · "));
                                if let Some(error) = &entry.error {
                                    a11y::live(
                                        &ui.add(
                                            egui::Label::new(
                                                RichText::new(error).size(12.5).color(p.error),
                                            )
                                            .wrap(),
                                        ),
                                    );
                                }
                                if !entry.verified && entry.error.is_none() {
                                    ui.label(
                                        RichText::new("Modified since creation.")
                                            .size(12.5)
                                            .color(p.warn),
                                    );
                                }
                            }
                        });
                    });
                });
            });
        let rect = shown.response.rect;
        let painter = ui.painter();
        if deleting {
            painter.rect_filled(
                egui::Rect::from_min_size(rect.min, egui::vec2(3.0, rect.height())),
                0.0,
                p.error,
            );
        }
        painter.hline(
            rect.left()..=rect.right(),
            rect.top(),
            egui::Stroke::new(1.0_f32, p.rule),
        );
        rect
    }

    pub(super) fn card_actions(
        &mut self,
        ui: &mut egui::Ui,
        entry: &sessions::Entry,
        listing: &Listing,
        busy: bool,
        waiting: bool,
        working: bool,
    ) {
        let path = entry.path.clone();
        let readable = entry.error.is_none();
        let open = |part| Target {
            path: path.clone(),
            part,
            at_s: None,
        };
        let more = widgets::icon_button(ui, ButtonKind::Secondary, Icon::More, "More");
        egui::Popup::menu(&more).show(|ui| {
            ui.set_min_width(200.0);
            if entry.summary
                && ui
                    .add_enabled(
                        readable && !busy && !waiting,
                        egui::Button::new("Summarise again"),
                    )
                    .clicked()
            {
                self.act(ui.ctx(), "summarise", Some(path.clone()));
                ui.close();
            }
            if entry.summary
                && ui
                    .add_enabled(!busy, egui::Button::new("Delete summary"))
                    .clicked()
            {
                self.act(ui.ctx(), "delete-summary", Some(path.clone()));
                ui.close();
            }
            if ui.button("Show in folder").clicked() {
                sessions::show_in_folder(&path);
                ui.close();
            }
            if ui
                .add_enabled(readable, egui::Button::new("Export…"))
                .clicked()
            {
                self.ask_export(path.clone(), Scope::Both, entry.summary);
                ui.close();
            }
            let part = if entry.summary {
                Part::Summary
            } else {
                Part::Transcript
            };
            if self.speaker.is_some() {
                let label = if self.reading(&path) {
                    "Stop reading"
                } else {
                    "Read aloud"
                };
                if ui.add_enabled(readable, egui::Button::new(label)).clicked() {
                    self.read_aloud(&path, part);
                    ui.close();
                }
            }
            for choice in [OpenIn::WordProcessor, OpenIn::TextEditor] {
                let label = format!("Open in {}", sessions::open_in_label(choice).to_lowercase());
                if ui.add_enabled(readable, egui::Button::new(label)).clicked() {
                    self.open_with(ui.ctx(), path.clone(), part, choice);
                    ui.close();
                }
            }
            ui.separator();
            let p = theme::palette(ui.ctx());
            if ui
                .add_enabled(
                    !busy,
                    egui::Button::new(RichText::new("Delete…").color(p.error)),
                )
                .clicked()
            {
                self.confirm_delete = Some(path.clone());
                ui.close();
            }
        });
        if entry.summary {
            if ui
                .add_enabled(readable, Button::tonal("Summary").small())
                .clicked()
            {
                self.stop_reading_unless(Some(&path));
                self.open(ui.ctx(), open(Part::Summary));
            }
        } else if ui
            .add_enabled(
                readable && !busy && !waiting,
                Button::secondary("Summarise").small(),
            )
            .clicked()
        {
            self.act(ui.ctx(), "summarise", Some(path.clone()));
        }
        if ui
            .add_enabled(readable, Button::secondary("Transcript").small())
            .clicked()
        {
            self.stop_reading_unless(Some(&path));
            self.open(ui.ctx(), open(Part::Transcript));
        }
        if entry.error.is_none() && (waiting || !entry.summary) {
            let text = listing.status(entry);
            let shown = if working {
                widgets::chip(ui, ChipKind::Ok, text)
            } else {
                widgets::chip(ui, ChipKind::Neutral, text)
            };
            a11y::live(&shown);
        }
    }
}

/// In-context hover preview for a session row: full title, capture details,
/// bundle path, and verification state without leaving the list.
fn session_hover_preview(response: &egui::Response, entry: &sessions::Entry, name: &str) {
    let title = name.to_owned();
    let mut facts = vec![entry.when()];
    facts.extend(entry.length());
    facts.extend(entry.source.clone());
    facts.extend(entry.playing.clone());
    let details = facts.join(" · ");
    let bundle = entry.path.display().to_string();
    let note = if let Some(err) = &entry.error {
        Some(err.clone())
    } else if !entry.verified {
        Some("Modified since creation.".to_owned())
    } else {
        None
    };
    response.clone().on_hover_ui(move |ui| {
        let p = theme::palette(ui.ctx());
        ui.set_max_width(widgets::popup_width(ui.ctx(), 280.0));
        ui.spacing_mut().item_spacing.y = 4.0;
        ui.add(
            egui::Label::new(
                RichText::new(&title)
                    .size(13.5)
                    .family(theme::semibold())
                    .color(p.text),
            )
            .wrap(),
        );
        widgets::small(ui, &details);
        ui.add(
            egui::Label::new(RichText::new(&bundle).monospace().size(11.5).color(p.muted)).wrap(),
        );
        if let Some(text) = &note {
            ui.add(egui::Label::new(RichText::new(text).size(12.0).color(p.warn)).wrap());
        }
    });
}
