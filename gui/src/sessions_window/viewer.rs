use super::*;

impl SessionsWindow {
    pub(super) fn viewer_header(
        &mut self,
        ui: &mut egui::Ui,
        state: &SessionsState,
        header: theme::Header,
    ) {
        let p = theme::palette(ui.ctx());
        let Some(target) = state.viewer.target.clone() else {
            return;
        };
        if ui
            .add(Button::ghost("All sessions").icon(Icon::ArrowLeft))
            .clicked()
        {
            self.state.lock().unwrap().viewer = Viewer::default();
            self.reading = None;
            self.refresh(ui.ctx());
            return;
        }
        let entry = match &state.listing {
            Some(Ok(listing)) => listing
                .sessions
                .iter()
                .find(|e| e.path == target.path)
                .cloned(),
            _ => None,
        };
        let title = entry.as_ref().and_then(|e| e.title.clone());
        let name = match (&title, &entry) {
            (Some(title), _) => title.clone(),
            (None, Some(entry)) => entry.when(),
            (None, None) => target
                .path
                .file_name()
                .map(|n| n.to_string_lossy().into_owned())
                .unwrap_or_default(),
        };
        ui.add_space(8.0);
        const ACTIONS_BREAK: f32 = 720.0;
        let has_summary = entry.as_ref().is_none_or(|e| e.summary);
        let can_open = target.part == Part::Transcript || has_summary;
        let subtitle = entry.as_ref().map(|entry| {
            let mut facts = Vec::new();
            if title.is_some() {
                facts.push(entry.when());
            }
            facts.extend(entry.length());
            facts.extend(entry.source.clone());
            facts.extend(entry.playing.clone());
            facts.join(" · ")
        });
        let draw_title = |ui: &mut egui::Ui| {
            ui.vertical(|ui| {
                ui.spacing_mut().item_spacing.y = 2.0;
                let shown = ui.add(
                    egui::Label::new(
                        RichText::new(&name)
                            .size(22.0)
                            .family(theme::bold())
                            .color(header.text),
                    )
                    .truncate(),
                );
                a11y::heading(&shown, &name, 2);
                if let Some(subtitle) = &subtitle
                    && !subtitle.is_empty()
                {
                    ui.add(
                        egui::Label::new(RichText::new(subtitle).size(12.5).color(header.muted))
                            .truncate(),
                    )
                    .on_hover_text(subtitle);
                }
            });
        };
        if ui.available_width() >= ACTIONS_BREAK {
            ui.horizontal(|ui| {
                ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                    if ui.add(Button::secondary("Show in folder")).clicked() {
                        sessions::show_in_folder(&target.path);
                    }
                    if ui.add(Button::secondary("Export…")).clicked() {
                        self.ask_export(target.path.clone(), target.part.into(), has_summary);
                    }
                    self.open_in_button(ui, &target.path, target.part, can_open);
                    ui.add_space(4.0);
                    ui.with_layout(Layout::top_down(Align::Min), |ui| {
                        let clip = ui.max_rect().intersect(ui.clip_rect());
                        ui.set_clip_rect(clip);
                        draw_title(ui);
                    });
                });
            });
        } else {
            draw_title(ui);
            ui.add_space(6.0);
            ui.horizontal_wrapped(|ui| {
                self.open_in_button(ui, &target.path, target.part, can_open);
                if ui.add(Button::secondary("Export…")).clicked() {
                    self.ask_export(target.path.clone(), target.part.into(), has_summary);
                }
                if ui.add(Button::secondary("Show in folder")).clicked() {
                    sessions::show_in_folder(&target.path);
                }
            });
        }
        ui.add_space(6.0);
        let text = match &state.viewer.text {
            Some(Ok(text)) => Some(text.clone()),
            _ => None,
        };
        let has_speaker = self.speaker.is_some();
        let is_reading = self.reading(&target.path);
        let mut switch_part = None;
        let mut do_copy = false;
        let mut do_read = false;
        widgets::responsive_bar(
            ui,
            420.0,
            |ui| {
                ui.spacing_mut().item_spacing.x = 18.0;
                a11y::tab_list(ui, "View");
                for (part, label) in [(Part::Transcript, "Transcript"), (Part::Summary, "Summary")]
                {
                    let enabled =
                        part == Part::Transcript || entry.as_ref().is_none_or(|e| e.summary);
                    if widgets::tab(ui, label, target.part == part, enabled, header).clicked()
                        && target.part != part
                    {
                        switch_part = Some(part);
                    }
                }
            },
            |ui| {
                let copy_label = if state.viewer.copied {
                    "Copied"
                } else {
                    "Copy"
                };
                let read_label = if is_reading { "Stop" } else { "Read aloud" };
                let rtl = ui.layout().main_dir() == egui::Direction::RightToLeft;
                if rtl {
                    if ui
                        .add_enabled(text.is_some(), Button::ghost(copy_label).small())
                        .clicked()
                    {
                        do_copy = true;
                    }
                    if has_speaker
                        && ui
                            .add_enabled(
                                text.is_some() || is_reading,
                                Button::tonal(read_label).small(),
                            )
                            .clicked()
                    {
                        do_read = true;
                    }
                } else {
                    if has_speaker
                        && ui
                            .add_enabled(
                                text.is_some() || is_reading,
                                Button::tonal(read_label).small(),
                            )
                            .clicked()
                    {
                        do_read = true;
                    }
                    if ui
                        .add_enabled(text.is_some(), Button::ghost(copy_label).small())
                        .clicked()
                    {
                        do_copy = true;
                    }
                }
            },
        );
        if let Some(part) = switch_part {
            self.open(
                ui.ctx(),
                Target {
                    path: target.path.clone(),
                    part,
                    at_s: None,
                },
            );
        }
        if do_copy && let Some(text) = &text {
            ui.ctx().copy_text(text.clone());
            self.state.lock().unwrap().viewer.copied = true;
        }
        if do_read {
            self.read_aloud(&target.path, target.part);
        }
        let _ = p;
    }

    pub(super) fn viewer(&mut self, ui: &mut egui::Ui, state: &SessionsState) {
        let Some(target) = state.viewer.target.clone() else {
            return;
        };
        let entry = match &state.listing {
            Some(Ok(listing)) => listing
                .sessions
                .iter()
                .find(|e| e.path == target.path)
                .cloned(),
            _ => None,
        };
        let title = entry.as_ref().and_then(|e| e.title.clone());
        self.export_notice(ui, state);
        self.open_notice(ui, state);
        self.export_panel(ui, &target.path);
        ui.add_space(8.0);
        let text = match &state.viewer.text {
            None => {
                a11y::live(&widgets::chip(ui, ChipKind::Neutral, "Reading…"));
                return;
            }
            Some(Err(why)) => {
                widgets::banner(ui, Tone::Error, why, |_| {});
                return;
            }
            Some(Ok(text)) => text.clone(),
        };
        match target.part {
            Part::Summary => self.summary(ui, &target, &text, title.as_deref()),
            Part::Transcript => self.transcript(ui, state, &target, &text),
        }
    }

    pub(super) fn stamped(
        &self,
        ui: &mut egui::Ui,
        target: &Target,
        line: &str,
        bold: bool,
    ) -> egui::Response {
        let styled = |text: &str| {
            let text = RichText::new(text).size(15.0);
            if bold {
                text.family(theme::semibold())
            } else {
                text
            }
        };
        let pieces = sessions::pieces(line);
        if !pieces.iter().any(|piece| matches!(piece, Piece::Stamp(_))) {
            return ui.add(egui::Label::new(styled(line)).wrap());
        }
        ui.horizontal_wrapped(|ui| {
            ui.spacing_mut().item_spacing.x = 0.0;
            let mut first: Option<egui::Response> = None;
            for piece in pieces {
                match piece {
                    Piece::Text(part) => {
                        let shown = ui.label(styled(&part));
                        first.get_or_insert(shown);
                    }
                    Piece::Stamp(seconds) => {
                        ui.add_space(4.0);
                        if stamp_pill(ui, seconds).clicked() {
                            self.open(
                                ui.ctx(),
                                Target {
                                    path: target.path.clone(),
                                    part: Part::Transcript,
                                    at_s: Some(seconds),
                                },
                            );
                        }
                        ui.add_space(4.0);
                    }
                }
            }
            first
        })
        .inner
        .unwrap_or_else(|| ui.response())
    }

    pub(super) fn summary(
        &self,
        ui: &mut egui::Ui,
        target: &Target,
        text: &str,
        title: Option<&str>,
    ) {
        let p = theme::palette(ui.ctx());
        ui.spacing_mut().item_spacing.y = 6.0;
        let mut first_heading = true;
        for line in text.lines() {
            match sessions::summary_line(line) {
                SummaryLine::Blank => ui.add_space(4.0),
                // (pinned by `sessions_window::tests::a_summary_opens_with_its_stamps_as_links_and_says_a_model_wrote_it`)
                SummaryLine::Heading(heading_text)
                    if first_heading && Some(heading_text.as_str()) == title =>
                {
                    first_heading = false;
                }
                SummaryLine::Heading(heading_text) => {
                    first_heading = false;
                    ui.add_space(10.0);
                    let shown = self.stamped(ui, target, &heading_text, true);
                    a11y::heading(&shown, &heading_text, 3);
                }
                SummaryLine::Bullet(item) => {
                    ui.horizontal_top(|ui| {
                        ui.spacing_mut().item_spacing.x = 10.0;
                        ui.label(RichText::new("•").size(15.0).color(p.muted));
                        ui.vertical(|ui| {
                            self.stamped(ui, target, &item, false);
                        });
                    });
                }
                SummaryLine::Footer(note) => {
                    ui.add_space(6.0);
                    widgets::small(ui, note);
                }
                SummaryLine::Text(line) => {
                    self.stamped(ui, target, &line, false);
                }
            }
        }
    }

    pub(super) fn transcript(
        &self,
        ui: &mut egui::Ui,
        state: &SessionsState,
        target: &Target,
        text: &str,
    ) {
        let p = theme::palette(ui.ctx());
        ui.spacing_mut().item_spacing.y = 4.0;
        let paragraphs = sessions::paragraphs(text);
        let wanted = target
            .at_s
            .and_then(|at| sessions::paragraph_at(&paragraphs, at));
        for (index, paragraph) in paragraphs.iter().enumerate() {
            let is_header = index == 0 && paragraph.start_s.is_none();
            if is_header {
                continue;
            }
            let marked = Some(index) == wanted;
            let mut job = egui::text::LayoutJob::default();
            job.wrap.max_width = ui.available_width() - 20.0;
            let body = egui::TextFormat {
                font_id: egui::FontId::proportional(15.0),
                color: p.text,
                line_height: Some(24.0),
                ..Default::default()
            };
            let stamped = paragraph
                .start_s
                .and_then(|_| paragraph.text.split_once("] "));
            let words = match stamped {
                Some((stamp, words)) => {
                    job.append(
                        &format!("{stamp}] "),
                        0.0,
                        egui::TextFormat {
                            font_id: egui::FontId::monospace(12.5),
                            color: p.muted,
                            ..body.clone()
                        },
                    );
                    words
                }
                None => paragraph.text.as_str(),
            };
            job.append(words, 0.0, body);
            let shown = egui::Frame::NONE
                .fill(if marked { p.tint } else { Color32::TRANSPARENT })
                .corner_radius(8.0)
                .inner_margin(egui::Margin::symmetric(10, 6))
                // Out by the padding, so the text lines up with the heading above it.
                .outer_margin(egui::Margin {
                    left: -10,
                    right: 0,
                    top: 0,
                    bottom: 0,
                })
                .show(ui, |ui| {
                    ui.add(egui::Label::new(job).wrap());
                });
            if marked && !state.viewer.jumped_to_stamp {
                shown.response.scroll_to_me(Some(Align::TOP));
                self.state.lock().unwrap().viewer.jumped_to_stamp = true;
            }
        }
    }
}
