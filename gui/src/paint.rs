use cosmic_text::{
    Attrs, Buffer, Color, Family, FontSystem, Metrics, Shaping, SwashCache, Weight, Wrap, fontdb,
};

use crate::captions::{Captions, Span, Tone};
use crate::dictation::Pill;
use crate::raster::{Canvas, Rect, Rgba};
use crate::settings::TextSize;

pub const LINES: usize = 2;

pub const BACKGROUND: Rgba = Rgba(14, 14, 16, 214);

pub fn tone_rgba(tone: Tone) -> Rgba {
    match tone {
        Tone::Caption => Rgba(250, 250, 250, 255),
        Tone::Pending => Rgba(160, 164, 172, 255),
        Tone::Dim => Rgba(142, 146, 154, 255),
        Tone::Good => Rgba(86, 204, 120, 255),
        Tone::Warn => Rgba(245, 184, 66, 255),
        Tone::Bad => Rgba(240, 96, 86, 255),
    }
}

fn tone_color(tone: Tone) -> Color {
    let Rgba(r, g, b, a) = tone_rgba(tone);
    Color::rgba(r, g, b, a)
}

#[derive(Debug, Clone, Copy)]
struct Geometry {
    caption_px: f32,
    caption_line: f32,
    status_px: f32,
    status_line: f32,
    pad_x: f32,
    pad_y: f32,
    gap: f32,
    radius: f32,
    max_width: f32,
}

impl Geometry {
    fn new(size: TextSize, scale: f32) -> Self {
        let px = size.caption_px();
        let status = (px * 0.5).max(12.0);
        Geometry {
            caption_px: px * scale,
            caption_line: (px * 1.35).round() * scale,
            status_px: status * scale,
            status_line: (status * 1.5).round() * scale,
            pad_x: (px * 0.8).round() * scale,
            pad_y: (px * 0.45).round() * scale,
            gap: (px * 0.2).round() * scale,
            radius: (px * 0.5).round() * scale,
            max_width: px * 36.0 * scale,
        }
    }

    fn height(&self) -> f32 {
        self.pad_y * 2.0 + self.status_line + self.gap + self.caption_line * LINES as f32
    }
}

pub fn logical_height(size: TextSize) -> u32 {
    Geometry::new(size, 1.0).height().ceil() as u32
}

pub const PILL_WIDTH: u32 = 640;
pub const PILL_HEIGHT: u32 = 44;

const PILL_TEXT_PX: f32 = 15.0;
const PILL_PAD: f32 = 16.0;
const PILL_ICON: f32 = 18.0;
const PILL_GAP: f32 = 10.0;

// Speech sits around 0.01-0.1 rms; square root so quiet talk still moves the bars.
fn meter(rms: f32) -> f32 {
    (rms / 0.08).sqrt().clamp(0.0, 1.0)
}

struct TextBlock {
    metrics: Metrics,
    weight: Weight,
    wrap: Wrap,
    left: f32,
    right: f32,
    top: f32,
    lines: usize,
}

pub struct Painter {
    fonts: FontSystem,
    glyphs: SwashCache,
}

impl Painter {
    pub fn new() -> Self {
        let mut fonts = FontSystem::new();
        prefer_an_installed_sans(fonts.db_mut());
        Painter {
            fonts,
            glyphs: SwashCache::new(),
        }
    }

    pub fn paint(&mut self, canvas: &mut Canvas, scale: f32, size: TextSize, captions: &Captions) {
        canvas.clear();
        let g = Geometry::new(size, scale);
        let width = canvas.width() as f32;
        let box_width = g.max_width.min(width).floor();
        let x0 = ((width - box_width) / 2.0).floor();
        let frame = Rect {
            x0,
            y0: 0.0,
            x1: x0 + box_width,
            y1: canvas.height() as f32,
        };
        canvas.fill_rounded(frame, g.radius, BACKGROUND);
        if captions.degraded() {
            canvas.stroke_rounded(
                frame,
                g.radius,
                (1.5 * scale).max(1.0),
                tone_rgba(Tone::Bad),
            );
        }

        let left = frame.x0 + g.pad_x;
        let right = frame.x1 - g.pad_x;

        let (dot, status) = captions.status();
        let top = frame.y0 + g.pad_y;
        let dot_radius = g.status_px * 0.3;
        canvas.fill_circle(
            left + dot_radius,
            top + g.status_line / 2.0,
            dot_radius,
            tone_rgba(dot),
        );
        self.draw_text(
            canvas,
            &status,
            TextBlock {
                metrics: Metrics::new(g.status_px, g.status_line),
                weight: Weight::NORMAL,
                wrap: Wrap::None,
                left: left + dot_radius * 2.0 + g.status_px * 0.5,
                right,
                top,
                lines: 1,
            },
        );

        self.draw_text(
            canvas,
            &captions.caption_spans(),
            TextBlock {
                metrics: Metrics::new(g.caption_px, g.caption_line),
                weight: Weight::MEDIUM,
                wrap: Wrap::WordOrGlyph,
                left,
                right,
                top: top + g.status_line + g.gap,
                lines: LINES,
            },
        );
    }

    pub fn paint_pill(&mut self, canvas: &mut Canvas, scale: f32, pill: &Pill) {
        canvas.clear();
        let (width, height) = (canvas.width() as f32, canvas.height() as f32);
        let px = PILL_TEXT_PX * scale;
        let line = (PILL_TEXT_PX * 1.4).round() * scale;
        let (pad, icon, gap) = (PILL_PAD * scale, PILL_ICON * scale, PILL_GAP * scale);
        let spans = [Span {
            text: pill.text.clone(),
            tone: Tone::Caption,
        }];
        let max_text = (width - pad * 2.0 - icon - gap).max(1.0);
        let text_width = self.measure(&spans, Metrics::new(px, line), max_text);

        let box_width = (pad * 2.0 + icon + gap + text_width).min(width).ceil();
        let x0 = ((width - box_width) / 2.0).floor();
        let frame = Rect {
            x0,
            y0: 0.0,
            x1: x0 + box_width,
            y1: height,
        };
        canvas.fill_rounded(frame, height / 2.0, BACKGROUND);

        let (cx, cy) = (x0 + pad + icon / 2.0, height / 2.0);
        let color = tone_rgba(pill.tone);
        match pill.level {
            Some(rms) => {
                let level = meter(rms);
                let bar = icon / 7.0;
                for (i, weight) in [0.55f32, 1.0, 0.75, 0.45].into_iter().enumerate() {
                    let tall = icon * (0.25 + 0.75 * (level * weight * 1.4).min(1.0));
                    let left = x0 + pad + i as f32 * bar * 2.0;
                    canvas.fill_rounded(
                        Rect {
                            x0: left,
                            y0: cy - tall / 2.0,
                            x1: left + bar,
                            y1: cy + tall / 2.0,
                        },
                        bar / 2.0,
                        color,
                    );
                }
            }
            None => canvas.fill_circle(cx, cy, icon * 0.3, color),
        }

        let left = x0 + pad + icon + gap;
        self.draw_text(
            canvas,
            &spans,
            TextBlock {
                metrics: Metrics::new(px, line),
                weight: Weight::MEDIUM,
                wrap: Wrap::None,
                left,
                right: (left + text_width + 1.0).min(frame.x1 - pad / 2.0),
                top: (height - line) / 2.0,
                lines: 1,
            },
        );
    }

    fn measure(&mut self, spans: &[Span], metrics: Metrics, max_width: f32) -> f32 {
        let mut buffer = Buffer::new(&mut self.fonts, metrics);
        buffer.set_wrap(Wrap::None);
        buffer.set_size(Some(max_width), None);
        let attrs = Attrs::new()
            .family(Family::SansSerif)
            .weight(Weight::MEDIUM);
        buffer.set_rich_text(
            spans.iter().map(|span| (span.text.as_str(), attrs.clone())),
            &attrs,
            Shaping::Advanced,
            None,
        );
        buffer.shape_until_scroll(&mut self.fonts, false);
        buffer
            .layout_runs()
            .map(|run| run.line_w)
            .fold(0.0, f32::max)
            .min(max_width)
    }

    fn draw_text(&mut self, canvas: &mut Canvas, spans: &[Span], block: TextBlock) {
        let mut buffer = Buffer::new(&mut self.fonts, block.metrics);
        buffer.set_wrap(block.wrap);
        buffer.set_size(Some((block.right - block.left).max(1.0)), None);
        let base = Attrs::new().family(Family::SansSerif).weight(block.weight);
        buffer.set_rich_text(
            spans.iter().map(|span| {
                (
                    span.text.as_str(),
                    base.clone().color(tone_color(span.tone)),
                )
            }),
            &base,
            Shaping::Advanced,
            None,
        );
        buffer.shape_until_scroll(&mut self.fonts, false);

        let runs: Vec<_> = buffer.layout_runs().collect();
        let skip = runs.len().saturating_sub(block.lines);
        let Some(first) = runs.get(skip) else {
            return;
        };
        let empty_lines = (block.lines - (runs.len() - skip)) as f32;
        let shift = block.top + empty_lines * block.metrics.line_height - first.line_top;
        let (clip_left, clip_right) = (block.left.floor() as i32, block.right.ceil() as i32);

        for run in &runs[skip..] {
            for glyph in run.glyphs {
                let physical = glyph.physical((block.left, run.line_y + shift), 1.0);
                let color = glyph.color_opt.unwrap_or(tone_color(Tone::Caption));
                self.glyphs.with_pixels(
                    &mut self.fonts,
                    physical.cache_key,
                    color,
                    |x, y, pixel| {
                        let px = physical.x + x;
                        if px < clip_left || px >= clip_right {
                            return;
                        }
                        let solid = Rgba(pixel.r(), pixel.g(), pixel.b(), 255);
                        canvas.blend(px, physical.y + y, solid, f32::from(pixel.a()) / 255.0);
                    },
                );
            }
        }
    }
}

/// fontconfig can name a sans that is not installed; fall back to one that is.
fn prefer_an_installed_sans(db: &mut fontdb::Database) {
    const PREFERRED: [&str; 6] = [
        "Inter",
        "Noto Sans",
        "Cantarell",
        "Ubuntu",
        "DejaVu Sans",
        "Liberation Sans",
    ];
    let installed = |db: &fontdb::Database, name: &str| {
        db.faces()
            .any(|face| face.families.iter().any(|(family, _)| family == name))
    };
    let current = db.family_name(&fontdb::Family::SansSerif).to_owned();
    if installed(db, &current) {
        return;
    }
    if let Some(name) = PREFERRED.into_iter().find(|name| installed(db, name)) {
        db.set_sans_serif_family(name);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::protocol::Event;

    fn painter_without_fonts() -> Painter {
        Painter {
            fonts: FontSystem::new_with_fonts([]),
            glyphs: SwashCache::new(),
        }
    }

    #[test]
    fn bigger_text_asks_for_a_taller_surface() {
        let heights: Vec<u32> = [TextSize::Small, TextSize::Medium, TextSize::Large]
            .into_iter()
            .map(logical_height)
            .collect();
        assert!(
            heights.windows(2).all(|pair| pair[0] < pair[1]),
            "{heights:?}"
        );
    }

    #[test]
    fn the_box_fits_two_caption_lines_and_the_status_line() {
        let g = Geometry::new(TextSize::Medium, 1.0);
        assert!(g.height() >= g.caption_line * 2.0 + g.status_line);
    }

    #[test]
    fn a_frame_paints_the_box_centred_and_leaves_the_sides_clear() {
        let (width, height) = (3000u32, logical_height(TextSize::Medium));
        let mut pixels = vec![0u8; (width * height * 4) as usize];
        let mut canvas = Canvas::new(&mut pixels, width, height);
        let mut captions = Captions::new();
        captions.apply(Event::Cycle {
            confirmed: vec!["hello".into()],
            pending: vec![],
            total_s: 1.0,
        });
        painter_without_fonts().paint(&mut canvas, 1.0, TextSize::Medium, &captions);

        let alpha = |x: u32, y: u32| pixels[((y * width + x) * 4 + 3) as usize];
        assert!(alpha(width / 2, height / 2) > 200, "the box is drawn");
        assert_eq!(
            alpha(0, height / 2),
            0,
            "a wide screen stays clear at the edges"
        );
    }

    #[test]
    fn the_pill_is_centred_and_clear_at_the_edges() {
        let (width, height) = (PILL_WIDTH, PILL_HEIGHT);
        let mut pixels = vec![0u8; (width * height * 4) as usize];
        let mut canvas = Canvas::new(&mut pixels, width, height);
        let pill = Pill {
            tone: Tone::Good,
            text: "Listening".into(),
            level: Some(0.05),
        };
        painter_without_fonts().paint_pill(&mut canvas, 1.0, &pill);
        let alpha = |x: u32, y: u32| pixels[((y * width + x) * 4 + 3) as usize];
        assert!(alpha(width / 2, height / 2) > 200);
        assert_eq!(alpha(0, height / 2), 0);
        assert_eq!(alpha(width - 1, height / 2), 0);
    }

    #[test]
    fn the_meter_moves_for_quiet_speech_and_saturates_for_loud() {
        assert!(meter(0.005) > 0.2);
        assert_eq!(meter(1.0), 1.0);
        assert_eq!(meter(0.0), 0.0);
    }

    #[test]
    fn painting_at_a_fractional_scale_stays_in_bounds() {
        let scale = 1.5;
        let height = (logical_height(TextSize::Large) as f32 * scale).round() as u32;
        let width = 900;
        let mut pixels = vec![0u8; (width * height * 4) as usize];
        let mut canvas = Canvas::new(&mut pixels, width, height);
        painter_without_fonts().paint(&mut canvas, scale, TextSize::Large, &Captions::new());
    }
}
