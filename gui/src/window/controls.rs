use super::*;

pub(super) const COMBO_WIDTH: f32 = 240.0;

pub(super) fn combo<T: Copy + PartialEq>(
    ui: &mut egui::Ui,
    id: &str,
    named: &dyn Fn(egui::Response) -> egui::Response,
    value: &mut T,
    all: &[T],
    label: fn(T) -> &'static str,
) -> bool {
    let before = *value;
    let shown = widgets::combo(ui, id, label(*value).to_owned(), COMBO_WIDTH).show_ui(ui, |ui| {
        for &option in all {
            ui.selectable_value(value, option, label(option));
        }
    });
    named(shown.response);
    *value != before
}

pub(super) fn segmented<T: Copy + PartialEq>(
    ui: &mut egui::Ui,
    name: &str,
    value: &mut T,
    all: &[T],
    label: fn(T) -> &'static str,
) -> bool {
    let options: Vec<(T, &str)> = all.iter().map(|&option| (option, label(option))).collect();
    widgets::segmented(ui, name, value, &options)
}

pub(super) fn number<N: egui::emath::Numeric>(
    ui: &mut egui::Ui,
    named: &dyn Fn(egui::Response) -> egui::Response,
    value: &mut N,
    range: std::ops::RangeInclusive<N>,
    unit: &str,
) -> bool {
    // Right to left: the unit after the box.
    widgets::small(ui, unit);
    let response = ui.add(egui::DragValue::new(value).range(range));
    named(response)
        // DragValue's own hover cursor is a horizontal resize arrow, which reads as a splitter here.
        .on_hover_cursor(egui::CursorIcon::Text)
        .changed()
}
