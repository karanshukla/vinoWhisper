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
    widgets::button_group(ui, name, value, &options)
}

pub(super) fn number(
    ui: &mut egui::Ui,
    named: &dyn Fn(egui::Response) -> egui::Response,
    value: &mut u64,
    range: std::ops::RangeInclusive<u64>,
    unit: &str,
    zero: &'static str,
) -> bool {
    // Laid out right to left, so the unit sits after the box.
    if *value > 0 {
        widgets::small(ui, unit);
    }
    let response = ui.add(
        egui::DragValue::new(value)
            .range(range)
            .custom_formatter(move |n, _| {
                if n == 0.0 {
                    zero.to_owned()
                } else {
                    format!("{n:.0}")
                }
            })
            .custom_parser(move |text| {
                let trimmed = text.trim();
                if trimmed.eq_ignore_ascii_case(zero) {
                    Some(0.0)
                } else {
                    trimmed.parse::<f64>().ok()
                }
            }),
    );
    named(response)
        // DragValue's resize arrow reads as a splitter here.
        .on_hover_cursor(egui::CursorIcon::Text)
        .changed()
}
