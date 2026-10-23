use crate::captions::Tone;
use crate::paint::{BACKGROUND, tone_rgba};
use crate::raster::{Canvas, Rect, Rgba};

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

// The box is drawn on the landing page's 64-unit grid, which leaves a 4-unit
// margin either side for its card layout. An installed icon doesn't want that
// margin: Plasma's task manager drew it visibly smaller than Dolphin's (#96).
// Crop to 2 units clear of the box, so only the antialiased edge has room.
const VIEW_BOX: (f32, f32, f32) = (2.0, 2.0, 60.0);

const EDGE: Rgba = Rgba(255, 255, 255, 46);
const EDGE_WIDTH: f32 = 2.0;

const MARKS: [(Shape, Tone); 6] = [
    (Shape::pill(12.0, 22.0, 6.0, 10.0), Tone::Good),
    (Shape::pill(22.0, 17.0, 6.0, 20.0), Tone::Good),
    (Shape::pill(32.0, 21.0, 6.0, 12.0), Tone::Good),
    (Shape::pill(42.0, 24.0, 10.0, 6.0), Tone::Caption),
    (Shape::pill(12.0, 41.0, 26.0, 6.0), Tone::Caption),
    (Shape::pill(42.0, 41.0, 10.0, 6.0), Tone::Pending),
];

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

// The symbolic icon sits straight on the panel, light or dark, so its voice
// marks use a darker shade of the overlay's green that keeps 3:1 against both.
const TRAY_GREEN: Rgba = Rgba(40, 150, 80, 255);

fn box_rgba() -> Rgba {
    Rgba(BACKGROUND.0, BACKGROUND.1, BACKGROUND.2, 255)
}

fn hex(Rgba(r, g, b, _): Rgba) -> String {
    format!("#{r:02x}{g:02x}{b:02x}")
}

pub fn app_svg() -> String {
    let (x, y, side) = VIEW_BOX;
    let mut svg =
        format!("<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"{x} {y} {side} {side}\">\n");
    let mut line = |element: String| {
        svg.push_str("  ");
        svg.push_str(&element);
        svg.push('\n');
    };
    line(BOX.svg_rect(&format!(r#"fill="{}""#, hex(box_rgba()))));
    // SVG strokes straddle the path; inset by half to match stroke_rounded.
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

pub fn symbolic_svg() -> String {
    let outline = TRAY_BOX.svg_path() + &TRAY_BOX.inset(TRAY_LINE).svg_path();
    let green = hex(TRAY_GREEN);
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
  <style id="current-color-scheme" type="text/css">.ColorScheme-Text {{ color: #232629; }}</style>
  <path class="ColorScheme-Text" style="fill:currentColor" fill-rule="evenodd" d="{outline}"/>
  <path style="fill:{green}" d="{voice}"/>
  <path class="ColorScheme-Text" style="fill:currentColor" d="{words}"/>
  <path class="ColorScheme-Text" style="fill:currentColor;fill-opacity:0.5" d="{pending}"/>
</svg>
"#
    )
}

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

    #[test]
    fn the_app_icon_fills_its_view_box_across() {
        let (x, y, side) = VIEW_BOX;
        let (left, right) = (BOX.x - x, x + side - (BOX.x + BOX.w));
        assert!((0.0..=2.0).contains(&left) && (0.0..=2.0).contains(&right));
        assert_eq!(left, right, "centred across");
        assert!(BOX.y >= y && BOX.y + BOX.h <= y + side, "nothing cropped");
    }

    #[test]
    fn the_tray_icon_lands_on_whole_pixels() {
        let shapes = std::iter::once(TRAY_BOX).chain(TRAY_MARKS.iter().map(|(shape, _)| *shape));
        for shape in shapes {
            for edge in [shape.x, shape.y, shape.w, shape.h, TRAY_LINE] {
                assert_eq!(edge.fract(), 0.0, "{shape:?} is off the pixel grid");
            }
        }
    }

    fn luminance(Rgba(r, g, b, _): Rgba) -> f32 {
        let channel = |c: u8| {
            let c = f32::from(c) / 255.0;
            if c <= 0.04045 {
                c / 12.92
            } else {
                ((c + 0.055) / 1.055).powf(2.4)
            }
        };
        0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)
    }

    #[test]
    fn the_symbolic_green_shows_on_light_and_dark_panels() {
        // Breeze Light and Breeze Dark panel backgrounds.
        for panel in [Rgba(239, 240, 241, 255), Rgba(32, 35, 38, 255)] {
            let (a, b) = (luminance(TRAY_GREEN) + 0.05, luminance(panel) + 0.05);
            let contrast = a.max(b) / a.min(b);
            assert!(contrast >= 3.0, "{panel:?}: only {contrast:.2}:1");
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
