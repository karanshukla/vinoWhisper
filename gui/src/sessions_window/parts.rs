use super::*;

// The window buttons' width past `header_frame`'s 20 inset, plus 12 before the row's last button.
pub(super) fn controls_room(ui: &egui::Ui) -> f32 {
    let room = vino_ui::window::controls_width() + vino_ui::window::BAR_GAP + 12.0;
    (room - 20.0 - ui.spacing().item_spacing.x).max(0.0)
}

pub(super) fn stamp_pill(ui: &mut egui::Ui, seconds: u64) -> egui::Response {
    let p = theme::palette(ui.ctx());
    let text = format!("[{}]", sessions::stamp(seconds));
    let galley = ui.painter().layout_no_wrap(
        text.clone(),
        egui::FontId::monospace(11.5),
        Color32::PLACEHOLDER,
    );
    let (rect, response) =
        ui.allocate_exact_size(galley.size() + egui::vec2(12.0, 4.0), egui::Sense::click());
    response.widget_info(|| egui::WidgetInfo::labeled(egui::Role::Link, true, &text));
    if ui.is_rect_visible(rect) {
        let painter = ui.painter();
        let fill = if response.hovered() {
            p.accent.gamma_multiply(0.3)
        } else {
            p.tint
        };
        painter.rect_filled(rect, 6.0, fill);
        painter.galley(rect.center() - galley.size() / 2.0, galley, p.accent);
        if response.has_focus() {
            painter.rect_stroke(
                rect.expand(2.0),
                8.0,
                egui::Stroke::new(2.0_f32, p.accent),
                egui::StrokeKind::Inside,
            );
        }
    }
    response.on_hover_cursor(egui::CursorIcon::PointingHand)
}

// A section's label: small caps with the count after it, read as "Sessions (12)".
pub(super) fn section(ui: &mut egui::Ui, title: &str, n: usize) {
    let p = theme::palette(ui.ctx());
    ui.add_space(14.0);
    let name = format!("{title} ({n})");
    let shown = ui
        .horizontal(|ui| {
            ui.spacing_mut().item_spacing.x = 6.0;
            let caps = ui.label(
                RichText::new(title.to_uppercase())
                    .size(12.0)
                    .family(theme::semibold())
                    .extra_letter_spacing(0.7)
                    .color(p.muted),
            );
            a11y::decorative(ui, caps.id);
            let count = ui.label(RichText::new(n.to_string()).size(12.0).color(p.muted));
            a11y::decorative(ui, count.id);
        })
        .response;
    a11y::heading(&shown, &name, 2);
    ui.add_space(6.0);
}
