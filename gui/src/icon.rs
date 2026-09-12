//! The icon: a caption box with two lines of text in it.
//!
//! Drawn in code for the tray rather than shipped as image files, so the
//! binary stays a single file. Plasma prefers the themed icon name the tray
//! also advertises; these pixmaps are for themes that lack it.

use crate::raster::{Canvas, Rect, Rgba};

/// For the launcher, installed by `--install`. The same drawing as
/// `draw` below, in the same 64-unit grid.
pub const APP_SVG: &str = r##"<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
  <rect x="4" y="10" width="56" height="44" rx="10" fill="#1d99f3"/>
  <rect x="13" y="26" width="26" height="6" rx="3" fill="#fff"/>
  <rect x="43" y="26" width="8" height="6" rx="3" fill="#fff" fill-opacity=".7"/>
  <rect x="13" y="37" width="14" height="6" rx="3" fill="#fff" fill-opacity=".7"/>
  <rect x="31" y="37" width="20" height="6" rx="3" fill="#fff"/>
</svg>
"##;

const BLUE: Rgba = Rgba(29, 153, 243, 255);
const WHITE: Rgba = Rgba(255, 255, 255, 255);
const SOFT: Rgba = Rgba(255, 255, 255, 178);

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
    let rect = |x: f32, y: f32, w: f32, h: f32| Rect {
        x0: x * unit,
        y0: y * unit,
        x1: (x + w) * unit,
        y1: (y + h) * unit,
    };
    canvas.fill_rounded(rect(4.0, 10.0, 56.0, 44.0), 10.0 * unit, BLUE);
    canvas.fill_rounded(rect(13.0, 26.0, 26.0, 6.0), 3.0 * unit, WHITE);
    canvas.fill_rounded(rect(43.0, 26.0, 8.0, 6.0), 3.0 * unit, SOFT);
    canvas.fill_rounded(rect(13.0, 37.0, 14.0, 6.0), 3.0 * unit, SOFT);
    canvas.fill_rounded(rect(31.0, 37.0, 20.0, 6.0), 3.0 * unit, WHITE);
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_size_is_a_complete_argb_image() {
        for icon in tray_icons() {
            assert_eq!(icon.data.len(), (icon.width * icon.height * 4) as usize);
        }
    }

    #[test]
    fn the_box_is_opaque_blue_and_the_corners_are_clear() {
        let icon = tray_icon(64);
        let pixel = |x: usize, y: usize| &icon.data[(y * 64 + x) * 4..(y * 64 + x) * 4 + 4];
        assert_eq!(
            pixel(8, 20),
            &[255, 29, 153, 243],
            "inside the box, clear of the bars"
        );
        assert_eq!(pixel(0, 0)[0], 0, "a corner is transparent");
    }
}
