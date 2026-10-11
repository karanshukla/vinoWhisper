//! Colours, type and shapes follow vinowhisper-landing's site.css: Inter, flat surfaces
//! separated by hairlines, 6px corners, a green that only ever means "this one".

use std::sync::Arc;

use eframe::egui::{
    self, Color32, CornerRadius, FontData, FontDefinitions, FontFamily, FontId, Stroke, TextStyle,
    Theme, Visuals,
};

pub struct Palette {
    pub bg: Color32,
    pub text: Color32,
    pub muted: Color32,
    pub rule: Color32,
    pub code_bg: Color32,
    pub link: Color32,
}

const fn rgb(hex: u32) -> Color32 {
    Color32::from_rgb((hex >> 16) as u8, (hex >> 8) as u8, hex as u8)
}

pub const DARK: Palette = Palette {
    bg: rgb(0x121315),
    text: rgb(0xe4e5e8),
    muted: rgb(0x9a9ea6),
    rule: rgb(0x2a2c30),
    code_bg: rgb(0x1e2023),
    link: rgb(0x7bdc97),
};

pub const LIGHT: Palette = Palette {
    bg: rgb(0xfcfcfc),
    text: rgb(0x1c1d20),
    muted: rgb(0x5d6068),
    rule: rgb(0xe2e3e6),
    code_bg: rgb(0xeef0f2),
    link: rgb(0x1a7040),
};

pub fn palette(ctx: &egui::Context) -> &'static Palette {
    match ctx.theme() {
        Theme::Dark => &DARK,
        Theme::Light => &LIGHT,
    }
}

pub fn bold() -> FontFamily {
    FontFamily::Name("Inter-Bold".into())
}

fn fonts() -> FontDefinitions {
    let mut fonts = FontDefinitions::default();
    for (name, bytes) in [
        (
            "Inter-Regular",
            &include_bytes!("../assets/fonts/Inter-Regular.ttf")[..],
        ),
        (
            "Inter-Bold",
            &include_bytes!("../assets/fonts/Inter-Bold.ttf")[..],
        ),
    ] {
        fonts
            .font_data
            .insert(name.to_owned(), Arc::new(FontData::from_static(bytes)));
    }
    // Regular leads the stock families so every widget picks it up; the default fonts stay
    // behind it for the glyphs the latin subset lacks.
    for family in [FontFamily::Proportional, FontFamily::Monospace] {
        fonts
            .families
            .entry(family)
            .or_default()
            .insert(0, "Inter-Regular".to_owned());
    }
    // Monospace is for commands and paths only, so keep the stock face first there.
    let mono = fonts.families.get_mut(&FontFamily::Monospace).unwrap();
    mono.remove(0);
    mono.push("Inter-Regular".to_owned());
    fonts.families.insert(
        bold(),
        vec!["Inter-Bold".to_owned(), "Inter-Regular".to_owned()],
    );
    fonts
}

fn visuals(p: &Palette, base: Visuals) -> Visuals {
    let corner = CornerRadius::same(6);
    let mut v = base;
    v.panel_fill = p.bg;
    v.window_fill = p.bg;
    v.extreme_bg_color = p.code_bg;
    v.faint_bg_color = p.code_bg;
    v.hyperlink_color = p.link;
    v.warn_fg_color = p.link;
    v.window_stroke = Stroke::new(1.0_f32, p.rule);
    v.selection.bg_fill = p.link.gamma_multiply(0.35);
    v.selection.stroke = Stroke::new(1.0_f32, p.link);

    v.widgets.noninteractive.bg_fill = p.bg;
    v.widgets.noninteractive.bg_stroke = Stroke::new(1.0_f32, p.rule);
    v.widgets.noninteractive.fg_stroke = Stroke::new(1.0_f32, p.text);
    for widget in [
        &mut v.widgets.inactive,
        &mut v.widgets.hovered,
        &mut v.widgets.active,
        &mut v.widgets.open,
    ] {
        widget.corner_radius = corner;
    }
    v.widgets.noninteractive.corner_radius = corner;
    v.widgets.inactive.bg_fill = p.code_bg;
    v.widgets.inactive.weak_bg_fill = p.code_bg;
    // halfway to muted: the unchecked box of a checkbox has to show against the page
    v.widgets.inactive.bg_stroke = Stroke::new(1.0_f32, p.rule.lerp_to_gamma(p.muted, 0.5));
    v.widgets.inactive.fg_stroke = Stroke::new(1.0_f32, p.text);
    v.widgets.hovered.bg_fill = p.code_bg;
    v.widgets.hovered.weak_bg_fill = p.code_bg;
    v.widgets.hovered.bg_stroke = Stroke::new(1.0_f32, p.muted);
    v.widgets.hovered.fg_stroke = Stroke::new(1.0_f32, p.text);
    v.widgets.active.bg_fill = p.code_bg;
    v.widgets.active.weak_bg_fill = p.code_bg;
    v.widgets.active.bg_stroke = Stroke::new(1.0_f32, p.link);
    v.widgets.active.fg_stroke = Stroke::new(1.0_f32, p.text);
    v.widgets.open.bg_fill = p.code_bg;
    v.widgets.open.weak_bg_fill = p.code_bg;
    v.widgets.open.bg_stroke = Stroke::new(1.0_f32, p.link);
    v
}

pub fn install(ctx: &egui::Context) {
    ctx.set_fonts(fonts());
    ctx.set_visuals_of(Theme::Dark, visuals(&DARK, Visuals::dark()));
    ctx.set_visuals_of(Theme::Light, visuals(&LIGHT, Visuals::light()));
    ctx.all_styles_mut(|style| {
        style
            .text_styles
            .insert(TextStyle::Body, FontId::proportional(15.0));
        style
            .text_styles
            .insert(TextStyle::Button, FontId::proportional(15.0));
        style
            .text_styles
            .insert(TextStyle::Small, FontId::proportional(13.0));
        style
            .text_styles
            .insert(TextStyle::Heading, FontId::new(30.0, bold()));
        style
            .text_styles
            .insert(TextStyle::Monospace, FontId::monospace(13.5));
        style.spacing.item_spacing = egui::vec2(10.0, 8.0);
        style.spacing.button_padding = egui::vec2(12.0, 6.0);
        style.spacing.interact_size.y = 30.0;
    });
}
