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

// The box sits on the landing page's 64-unit grid. Plasma's task manager drew the grid's
// full margin visibly smaller than Dolphin did, so crop to just the antialiased edge.
const VIEW_CLEARANCE: f32 = 2.0;
const VIEW_SIDE: f32 = BOX.w + 2.0 * VIEW_CLEARANCE;
const VIEW_X: f32 = BOX.x - VIEW_CLEARANCE;
const VIEW_Y: f32 = BOX.y + BOX.h / 2.0 - VIEW_SIDE / 2.0;

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

// The tray shows only the voice bars: with the caption box around them it read as vinoWatch's icon.
const BARS: [Shape; 3] = [
    Shape {
        x: 6.0,
        y: 20.0,
        w: 12.0,
        h: 24.0,
        r: 6.0,
    },
    Shape {
        x: 26.0,
        y: 6.0,
        w: 12.0,
        h: 52.0,
        r: 6.0,
    },
    Shape {
        x: 46.0,
        y: 16.0,
        w: 12.0,
        h: 32.0,
        r: 6.0,
    },
];

const TRAY_BARS: [Shape; 3] = [
    Shape::line(3.0, 5.0, 2.0, 6.0),
    Shape::line(7.0, 2.0, 2.0, 12.0),
    Shape::line(11.0, 4.0, 2.0, 8.0),
];

// Darker than the overlay's green: the symbolic icon sits on light and dark panels alike
// (pinned by `tests::the_symbolic_green_shows_on_light_and_dark_panels`).
const TRAY_GREEN: Rgba = Rgba(40, 150, 80, 255);

fn box_rgba() -> Rgba {
    Rgba(BACKGROUND.0, BACKGROUND.1, BACKGROUND.2, 255)
}

fn hex(Rgba(r, g, b, _): Rgba) -> String {
    format!("#{r:02x}{g:02x}{b:02x}")
}

pub fn app_svg() -> String {
    let mut svg = format!(
        "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"{VIEW_X} {VIEW_Y} {VIEW_SIDE} {VIEW_SIDE}\">\n"
    );
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
    let green = hex(TRAY_GREEN);
    let bars: String = TRAY_BARS.iter().map(|bar| bar.svg_path()).collect();
    format!(
        r#"<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">
  <path style="fill:{green}" d="{bars}"/>
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
    for bar in BARS {
        canvas.fill_rounded(bar.rect(unit), bar.r * unit, tone_rgba(Tone::Good));
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
    fn the_tray_icon_is_bars_on_a_clear_ground() {
        let icon = tray_icon(64);
        let pixel = |x: usize, y: usize| &icon.data[(y * 64 + x) * 4..(y * 64 + x) * 4 + 4];
        assert_eq!(pixel(0, 0)[0], 0, "a corner is transparent");
        assert_eq!(pixel(32, 32)[0], 255, "the middle bar is solid");
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
    }

    #[test]
    fn the_app_icon_fills_its_view_box_across() {
        let (x, y, side) = (VIEW_X, VIEW_Y, VIEW_SIDE);
        let (left, right) = (BOX.x - x, x + side - (BOX.x + BOX.w));
        assert!((0.0..=2.0).contains(&left) && (0.0..=2.0).contains(&right));
        assert_eq!(left, right, "centred across");
        assert!(BOX.y >= y && BOX.y + BOX.h <= y + side, "nothing cropped");
    }

    #[test]
    fn the_tray_icon_lands_on_whole_pixels() {
        for shape in TRAY_BARS {
            for edge in [shape.x, shape.y, shape.w, shape.h] {
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

    const BREEZE_LIGHT_PANEL: Rgba = Rgba(239, 240, 241, 255);
    const BREEZE_DARK_PANEL: Rgba = Rgba(32, 35, 38, 255);

    #[test]
    fn the_symbolic_green_shows_on_light_and_dark_panels() {
        for panel in [BREEZE_LIGHT_PANEL, BREEZE_DARK_PANEL] {
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
