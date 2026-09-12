//! The mark: speech turning into words, inside the caption box.
//!
//! Three voice bars, in the green of the overlay's listening dot, become a
//! word, above a line of confirmed text that ends in a pending one. The box is
//! the overlay's own background, so the icon looks like what it opens.
//!
//! Drawn as data. The tray's pixmaps, the launcher's SVG and the README's
//! copy come from `BOX` and `MARKS`. The symbolic tray icon is the same
//! composition redrawn as `TRAY_BOX` and `TRAY_MARKS`, in one-pixel lines on
//! Breeze's 16px grid: the panel's other icons are outlines, and the 64-unit
//! shapes shrunk to 16px became a solid block that stood out among them
//! (tried first, 2026-09-12). Both SVGs in `docs/assets/` are generated:
//! `VINOWHISPER_BLESS_ICONS=1 cargo test` rewrites them, and a test fails when
//! they go stale.

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

    /// Square-ended, for lines a pixel wide, where rounding would only blur.
    const fn line(x: f32, y: f32, w: f32, h: f32) -> Shape {
        Shape { x, y, w, h, r: 0.0 }
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
        if r <= 0.0 {
            return format!("M{x} {y}h{w}v{h}h-{w}z");
        }
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

/// `BOX` and `MARKS` again, for the tray, on a 16px grid with every edge on a
/// whole pixel. The box is an outline `TRAY_LINE` wide, like Breeze's.
const TRAY_BOX: Shape = Shape {
    x: 1.0,
    y: 2.0,
    w: 14.0,
    h: 12.0,
    r: 2.0,
};
const TRAY_LINE: f32 = 1.0;

const TRAY_MARKS: [(Shape, Tone); 6] = [
    (Shape::line(4.0, 5.0, 1.0, 3.0), Tone::Good),
    (Shape::line(6.0, 4.0, 1.0, 5.0), Tone::Good),
    (Shape::line(8.0, 5.0, 1.0, 3.0), Tone::Good),
    (Shape::line(10.0, 6.0, 3.0, 1.0), Tone::Caption),
    (Shape::line(4.0, 11.0, 6.0, 1.0), Tone::Caption),
    (Shape::line(11.0, 11.0, 2.0, 1.0), Tone::Pending),
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

/// For the tray, in Breeze's style: a one-pixel outline with the voice in the
/// accent colour, the way Breeze's own subtitles icon uses it.
///
/// The stylesheet is KDE's convention, so Plasma paints `Text` and `Accent`
/// from the colour scheme; anything else draws the fallbacks. The outline is
/// the box minus its inset by the even-odd rule rather than a stroke, so it
/// lands on whole pixels whatever the renderer does with stroke alignment.
pub fn symbolic_svg() -> String {
    let outline = TRAY_BOX.svg_path() + &TRAY_BOX.inset(TRAY_LINE).svg_path();
    let (mut voice, mut words, mut pending) = (String::new(), String::new(), String::new());
    for (shape, tone) in TRAY_MARKS {
        match tone {
            Tone::Good => &mut voice,
            Tone::Pending => &mut pending,
            _ => &mut words,
        }
        .push_str(&shape.svg_path());
    }
    format!(
        r#"<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">
  <style id="current-color-scheme" type="text/css">.ColorScheme-Text {{ color: #232629; }} .ColorScheme-Accent {{ color: #3daee9; }}</style>
  <path class="ColorScheme-Text" style="fill:currentColor" fill-rule="evenodd" d="{outline}"/>
  <path class="ColorScheme-Accent" style="fill:currentColor" d="{voice}"/>
  <path class="ColorScheme-Text" style="fill:currentColor" d="{words}"/>
  <path class="ColorScheme-Text" style="fill:currentColor;fill-opacity:0.5" d="{pending}"/>
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

    /// A mark touching the box's edge merges into it, and two touching marks
    /// read as one, which at 16px is most of the drawing.
    fn assert_inside_and_apart(frame: Shape, edge: f32, marks: &[(Shape, Tone)]) {
        let inner = frame.inset(edge);
        for (a, _) in marks {
            assert!(
                a.x > inner.x
                    && a.y > inner.y
                    && a.x + a.w < inner.x + inner.w
                    && a.y + a.h < inner.y + inner.h,
                "{a:?} reaches the edge"
            );
            assert!(a.r * 2.0 <= a.w.min(a.h), "{a:?} is rounded past a pill");
        }
        for (i, (a, _)) in marks.iter().enumerate() {
            for (b, _) in &marks[i + 1..] {
                let apart =
                    a.x + a.w < b.x || b.x + b.w < a.x || a.y + a.h < b.y || b.y + b.h < a.y;
                assert!(apart, "{a:?} touches {b:?}");
            }
        }
    }

    #[test]
    fn the_marks_sit_inside_the_box_and_apart() {
        assert_inside_and_apart(BOX, EDGE_WIDTH, &MARKS);
        assert_inside_and_apart(TRAY_BOX, TRAY_LINE, &TRAY_MARKS);
    }

    /// A one-pixel line on a half-pixel edge renders as two grey ones, which
    /// is the blur Breeze's grid exists to avoid.
    #[test]
    fn the_tray_icon_lands_on_whole_pixels() {
        let shapes = std::iter::once(TRAY_BOX).chain(TRAY_MARKS.iter().map(|(shape, _)| *shape));
        for shape in shapes {
            for edge in [shape.x, shape.y, shape.w, shape.h, TRAY_LINE] {
                assert_eq!(edge.fract(), 0.0, "{shape:?} is off the pixel grid");
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
