//! The mark: speech turning into words, inside the caption box.
//!
//! Three voice bars, in the green of the overlay's listening dot, become a
//! word, above a line of confirmed text that ends in a pending one. The box is
//! the overlay's own background, so the icon looks like what it opens.
//!
//! Drawn once, as data. The tray's pixmaps, the launcher's SVG and the
//! symbolic tray icon all come from `BOX` and `MARKS`, so they cannot drift
//! apart, and the copies in `docs/assets/` (for the README) are generated
//! from them too: `VINOWHISPER_BLESS_ICONS=1 cargo test` rewrites them, and a
//! test fails when they go stale.

use crate::captions::Tone;
use crate::paint::{BACKGROUND, tone_rgba};
use crate::raster::{Canvas, Rect, Rgba};

/// A rounded rectangle in the icon's 64-unit grid. Everything in the mark is
/// one.
#[derive(Debug, Clone, Copy)]
struct Shape {
    x: f32,
    y: f32,
    w: f32,
    h: f32,
    r: f32,
}

impl Shape {
    const fn pill(x: f32, y: f32, w: f32, h: f32) -> Shape {
        Shape { x, y, w, h, r: 3.0 }
    }

    fn inset(self, by: f32) -> Shape {
        Shape {
            x: self.x + by,
            y: self.y + by,
            w: self.w - by * 2.0,
            h: self.h - by * 2.0,
            r: self.r - by,
        }
    }

    fn rect(self, unit: f32) -> Rect {
        Rect {
            x0: self.x * unit,
            y0: self.y * unit,
            x1: (self.x + self.w) * unit,
            y1: (self.y + self.h) * unit,
        }
    }

    fn svg_rect(self, paint: &str) -> String {
        let Shape { x, y, w, h, r } = self;
        format!(r#"<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" {paint}/>"#)
    }

    /// One closed subpath, for the symbolic icon's single path.
    fn svg_path(self) -> String {
        let Shape { x, y, w, h, r } = self;
        let (across, down) = (w - r * 2.0, h - r * 2.0);
        format!(
            "M{} {y}h{across}a{r} {r} 0 0 1 {r} {r}v{down}a{r} {r} 0 0 1 -{r} {r}\
             h-{across}a{r} {r} 0 0 1 -{r} -{r}v-{down}a{r} {r} 0 0 1 {r} -{r}z",
            x + r
        )
    }
}

const BOX: Shape = Shape {
    x: 4.0,
    y: 10.0,
    w: 56.0,
    h: 44.0,
    r: 12.0,
};

/// A faint light edge inside the box, so a dark box still has an outline on a
/// dark panel or menu.
const EDGE: Rgba = Rgba(255, 255, 255, 46);
const EDGE_WIDTH: f32 = 2.0;

const MARKS: [(Shape, Tone); 6] = [
    // The voice...
    (Shape::pill(12.0, 22.0, 6.0, 10.0), Tone::Good),
    (Shape::pill(22.0, 17.0, 6.0, 20.0), Tone::Good),
    (Shape::pill(32.0, 21.0, 6.0, 12.0), Tone::Good),
    // ...becoming a word,
    (Shape::pill(42.0, 24.0, 10.0, 6.0), Tone::Caption),
    // and a confirmed line that ends in a word still pending.
    (Shape::pill(12.0, 41.0, 26.0, 6.0), Tone::Caption),
    (Shape::pill(42.0, 41.0, 10.0, 6.0), Tone::Pending),
];

/// The overlay's box colour, without the translucency it needs over video.
fn box_rgba() -> Rgba {
    Rgba(BACKGROUND.0, BACKGROUND.1, BACKGROUND.2, 255)
}

fn hex(Rgba(r, g, b, _): Rgba) -> String {
    format!("#{r:02x}{g:02x}{b:02x}")
}

/// For the launcher, installed by `--install` and on first run.
pub fn app_svg() -> String {
    let mut svg =
        String::from("<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 64 64\">\n");
    let mut line = |element: String| {
        svg.push_str("  ");
        svg.push_str(&element);
        svg.push('\n');
    };
    line(BOX.svg_rect(&format!(r#"fill="{}""#, hex(box_rgba()))));
    // An SVG stroke straddles its path and `stroke_rounded` draws inside the
    // rectangle, so the SVG's is inset by half its width to match.
    line(BOX.inset(EDGE_WIDTH / 2.0).svg_rect(&format!(
        r#"fill="none" stroke="{}" stroke-opacity="{:.2}" stroke-width="{EDGE_WIDTH}""#,
        hex(EDGE),
        f32::from(EDGE.3) / 255.0
    )));
    for (shape, tone) in MARKS {
        line(shape.svg_rect(&format!(r#"fill="{}""#, hex(tone_rgba(tone)))));
    }
    svg.push_str("</svg>\n");
    svg
}

/// For the tray: the same shapes in one colour, the marks cut out of the box.
///
/// The stylesheet is KDE's convention, so Plasma paints it in the panel's text
/// colour; anything else draws the fallback grey. Holes come from the even-odd
/// rule rather than a mask, because Qt only promises SVG Tiny, which has none.
pub fn symbolic_svg() -> String {
    let path: String = std::iter::once(BOX)
        .chain(MARKS.iter().map(|(shape, _)| *shape))
        .map(Shape::svg_path)
        .collect();
    format!(
        r#"<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
  <style id="current-color-scheme" type="text/css">.ColorScheme-Text {{ color: #232629; }}</style>
  <path class="ColorScheme-Text" style="fill:currentColor" fill-rule="evenodd" d="{path}"/>
</svg>
"#
    )
}

/// For a tray that cannot find the symbolic icon by name.
pub fn tray_icons() -> Vec<ksni::Icon> {
    [16, 22, 24, 32, 48, 64]
        .into_iter()
        .map(tray_icon)
        .collect()
}

fn tray_icon(size: u32) -> ksni::Icon {
    let mut pixels = vec![0u8; (size * size * 4) as usize];
    let mut canvas = Canvas::new(&mut pixels, size, size);
    draw(&mut canvas, size as f32);
    ksni::Icon {
        width: size as i32,
        height: size as i32,
        data: canvas.to_argb32_be(),
    }
}

fn draw(canvas: &mut Canvas, size: f32) {
    let unit = size / 64.0;
    canvas.fill_rounded(BOX.rect(unit), BOX.r * unit, box_rgba());
    canvas.stroke_rounded(BOX.rect(unit), BOX.r * unit, EDGE_WIDTH * unit, EDGE);
    for (shape, tone) in MARKS {
        canvas.fill_rounded(shape.rect(unit), shape.r * unit, tone_rgba(tone));
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::Path;

    #[test]
    fn every_size_is_a_complete_argb_image() {
        for icon in tray_icons() {
            assert_eq!(icon.data.len(), (icon.width * icon.height * 4) as usize);
        }
    }

    #[test]
    fn the_box_is_the_overlays_colour_and_the_corners_are_clear() {
        let icon = tray_icon(64);
        let pixel = |x: usize, y: usize| &icon.data[(y * 64 + x) * 4..(y * 64 + x) * 4 + 4];
        let Rgba(r, g, b, _) = BACKGROUND;
        assert_eq!(
            pixel(30, 14),
            &[255, r, g, b],
            "inside the box, clear of the edge and the marks"
        );
        assert_eq!(pixel(0, 0)[0], 0, "a corner is transparent");
    }

    /// The symbolic icon cuts every mark out of the box with the even-odd
    /// rule, so two marks that touched would fill each other back in, and a
    /// mark over the edge would show as a notch.
    #[test]
    fn the_marks_sit_inside_the_box_and_apart() {
        let inner = BOX.inset(EDGE_WIDTH);
        for (a, _) in MARKS {
            assert!(
                a.x > inner.x
                    && a.y > inner.y
                    && a.x + a.w < inner.x + inner.w
                    && a.y + a.h < inner.y + inner.h,
                "{a:?} reaches the edge"
            );
            assert!(a.r * 2.0 <= a.w.min(a.h), "{a:?} is rounded past a pill");
        }
        for (i, (a, _)) in MARKS.iter().enumerate() {
            for (b, _) in &MARKS[i + 1..] {
                let apart =
                    a.x + a.w < b.x || b.x + b.w < a.x || a.y + a.h < b.y || b.y + b.h < a.y;
                assert!(apart, "{a:?} touches {b:?}");
            }
        }
    }

    #[test]
    fn the_checked_in_icons_are_the_ones_drawn_here() {
        let assets = Path::new(env!("CARGO_MANIFEST_DIR")).join("../docs/assets");
        let bless = std::env::var_os("VINOWHISPER_BLESS_ICONS").is_some();
        for (name, svg) in [
            ("vinowhisper.svg", app_svg()),
            ("vinowhisper-symbolic.svg", symbolic_svg()),
        ] {
            let path = assets.join(name);
            if bless {
                std::fs::create_dir_all(&assets).unwrap();
                std::fs::write(&path, &svg).unwrap();
            }
            let on_disk = std::fs::read_to_string(&path).unwrap_or_default();
            assert!(
                on_disk == svg,
                "{} is stale; regenerate it with VINOWHISPER_BLESS_ICONS=1 cargo test",
                path.display()
            );
        }
    }
}
