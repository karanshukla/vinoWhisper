use super::*;

impl SettingsWindow {
    pub(super) fn refresh_echo_line(&self, ctx: &egui::Context) {
        let line = Arc::clone(&self.echo_line);
        let ctx = ctx.clone();
        std::thread::spawn(move || {
            *line.lock().unwrap() = Some(crate::echo::status(EchoCancel::Auto));
            ctx.request_repaint();
        });
    }

    pub(super) fn captions(&mut self, ui: &mut egui::Ui) -> bool {
        let [overlay, listening] = widgets::columns(ui, |ui, column| match column {
            Column::Left => self.overlay(ui),
            Column::Right => self.listening(ui),
        });
        overlay | listening
    }

    fn overlay(&mut self, ui: &mut egui::Ui) -> bool {
        let mut changed = false;
        widgets::group_label(ui, "Overlay");
        widgets::group(ui, |ui| {
            widgets::group_section(ui, |ui| {
                caption_preview(ui, self.settings.position, self.settings.size);
                widgets::small(ui, "Preview");
            });
            Row::new("Position").show(ui, |ui, _| {
                changed |= segmented(
                    ui,
                    "Position",
                    &mut self.settings.position,
                    &Position::ALL,
                    position_label,
                );
            });
            Row::new("Text size").show(ui, |ui, _| {
                changed |= segmented(
                    ui,
                    "Text size",
                    &mut self.settings.size,
                    &TextSize::ALL,
                    size_label,
                );
            });
        });
        changed
    }

    fn listening(&mut self, ui: &mut egui::Ui) -> bool {
        let mut changed = false;
        widgets::group_label(ui, "Listening");
        widgets::group(ui, |ui| {
            Row::new("Listen to")
                .help("What the captions follow. Both labels your words you and the call them.")
                .show(ui, |ui, _| {
                    changed |= segmented(
                        ui,
                        "Listen to",
                        &mut self.settings.source,
                        &Source::ALL,
                        source_label,
                    );
                });
            Row::new("Echo cancellation")
                .help(
                    "Both only: takes their voice out of your microphone, so a call on speakers \
                     does not caption it twice. Applies from the next session.",
                )
                .show(ui, |ui, _| {
                    changed |= segmented(
                        ui,
                        "Echo cancellation",
                        &mut self.settings.echo_cancel,
                        &EchoCancel::ALL,
                        echo_label,
                    );
                    if let Some(Ok(line)) = self.echo_line.lock().unwrap().as_ref()
                        && self.settings.echo_cancel == EchoCancel::Auto
                    {
                        widgets::small(ui, line);
                    }
                });
            Row::new("Hide after silence")
                .help("0 keeps them up.")
                .show(ui, |ui, named| {
                    changed |= number(
                        ui,
                        named,
                        &mut self.settings.hide_after_silence_s,
                        0..=3600,
                        "s",
                    );
                });
            Row::new("Save transcripts")
                .help("Keep each session to read later.")
                .show(ui, |ui, _| {
                    changed |= widgets::switch(
                        ui,
                        &mut self.settings.save_transcripts,
                        "Save transcripts",
                    )
                    .changed();
                });
        });
        widgets::group_label(ui, "Tray");
        widgets::group(ui, |ui| {
            Row::new("Hide icon after")
                .help("Moves to hidden icons when idle. 0 keeps it in view.")
                .show(ui, |ui, named| {
                    changed |= number(
                        ui,
                        named,
                        &mut self.settings.tray_idle_minutes,
                        0..=1440,
                        "min",
                    );
                });
        });
        changed
    }
}

pub(super) fn caption_preview(ui: &mut egui::Ui, position: Position, size: TextSize) {
    let p = theme::palette(ui.ctx());
    let height = 132.0;
    let (rect, response) = ui.allocate_exact_size(
        egui::vec2(ui.available_width(), height),
        egui::Sense::hover(),
    );
    a11y::decorative(ui, response.id);
    if !ui.is_rect_visible(rect) {
        return;
    }
    let painter = ui.painter();
    painter.rect(
        rect,
        8.0,
        p.field,
        egui::Stroke::new(1.0_f32, p.rule),
        egui::StrokeKind::Inside,
    );
    for (y, w) in [(12.0, 120.0), (28.0, 200.0)] {
        painter.rect_filled(
            egui::Rect::from_min_size(rect.min + egui::vec2(14.0, y), egui::vec2(w, 8.0)),
            4.0,
            p.pressed,
        );
    }
    let text_size = match size {
        TextSize::Small => 12.0,
        TextSize::Medium => 14.0,
        TextSize::Large => 17.0,
    };
    let galley = painter.layout_no_wrap(
        "…and that's why the release moved to Thursday.".to_owned(),
        egui::FontId::proportional(text_size),
        egui::Color32::PLACEHOLDER,
    );
    let box_w = (galley.size().x + 28.0).min(rect.width() * 0.72);
    let box_h = galley.size().y + 16.0;
    let y = match position {
        Position::Bottom => rect.bottom() - 14.0 - box_h,
        Position::Top => rect.top() + 14.0,
    };
    let caption = egui::Rect::from_min_size(
        egui::pos2(rect.center().x - box_w / 2.0, y),
        egui::vec2(box_w, box_h),
    );
    painter.rect_filled(
        caption,
        10.0,
        egui::Color32::from_rgba_unmultiplied(14, 14, 16, 224),
    );
    let clipped = painter.with_clip_rect(caption.shrink2(egui::vec2(14.0, 0.0)));
    clipped.galley(
        egui::pos2(
            caption.left() + 14.0,
            caption.center().y - galley.size().y / 2.0,
        ),
        galley,
        egui::Color32::from_rgb(0xfa, 0xfa, 0xfa),
    );
}
