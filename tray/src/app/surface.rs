use super::*;

const SIDE_MARGIN: i32 = 16;
const EDGE_MARGIN: i32 = 48;
const PILL_GAP: i32 = 12;

pub(super) struct Overlay {
    pub(super) layer: LayerSurface,
    pub(super) fractional: Option<WpFractionalScaleV1>,
    pub(super) viewport: Option<WpViewport>,
    pub(super) width: u32,
    pub(super) height: u32,
    pub(super) configured: bool,
    pub(super) scale120: u32,
    pub(super) buffer_scale: i32,
}

impl Overlay {
    pub(super) fn scale(&self) -> f64 {
        if self.viewport.is_some() {
            f64::from(self.scale120) / 120.0
        } else {
            f64::from(self.buffer_scale)
        }
    }
}

impl Drop for Overlay {
    fn drop(&mut self) {
        if let Some(viewport) = self.viewport.take() {
            viewport.destroy();
        }
        if let Some(fractional) = self.fractional.take() {
            fractional.destroy();
        }
    }
}

impl App {
    pub(super) fn create_overlay(&mut self) {
        self.overlay = Some(self.create_layer("vinowhisper", |layer, app| {
            place(layer, &app.settings);
        }));
    }

    pub(super) fn create_layer(
        &self,
        namespace: &str,
        place: impl FnOnce(&LayerSurface, &App),
    ) -> Overlay {
        let surface = self.compositor.create_surface(&self.qh);
        let layer = self.layer_shell.create_layer_surface(
            &self.qh,
            surface,
            Layer::Overlay,
            Some(namespace),
            None,
        );
        layer.set_keyboard_interactivity(KeyboardInteractivity::None);
        // Zero, not -1: stay clear of panels.
        layer.set_exclusive_zone(0);
        place(&layer, self);
        // Empty input region: click-through, so it never blocks the video under it.
        match Region::new(&self.compositor) {
            Ok(region) => layer
                .wl_surface()
                .set_input_region(Some(region.wl_region())),
            Err(err) => eprintln!("[vinowhisper-tray] overlay will catch clicks: {err}"),
        }
        let (fractional, viewport) = match &self.scaling {
            Some((manager, viewporter)) => (
                Some(manager.get_fractional_scale(layer.wl_surface(), &self.qh, ())),
                Some(viewporter.get_viewport(layer.wl_surface(), &self.qh, ())),
            ),
            None => (None, None),
        };
        layer.commit();
        Overlay {
            layer,
            fractional,
            viewport,
            width: 0,
            height: 0,
            configured: false,
            scale120: 120,
            buffer_scale: 1,
        }
    }

    pub(super) fn place(&mut self) {
        if let Some(overlay) = &self.overlay {
            place(&overlay.layer, &self.settings);
            overlay.layer.commit();
        }
        self.place_pill();
    }

    pub(super) fn place_pill(&mut self) {
        if let Some(pill) = &self.pill {
            place_pill(&pill.layer, &self.settings, self.visible);
            pill.layer.commit();
        }
    }

    pub(super) fn draw_pill(&mut self) {
        let (Some(pill), Some(content)) = (&self.pill, self.dictation.pill()) else {
            return;
        };
        let painter = &mut self.painter;
        render(&mut self.pool, pill, |canvas, scale| {
            painter.paint_pill(canvas, scale, &content);
        });
    }

    pub(super) fn draw(&mut self) {
        let Some(overlay) = &self.overlay else {
            return;
        };
        let (painter, size, captions) = (&mut self.painter, self.settings.size, &self.captions);
        render(&mut self.pool, overlay, |canvas, scale| {
            painter.paint(canvas, scale, size, captions);
        });
    }

    pub(super) fn redraw(&mut self, surface: &wl_surface::WlSurface) {
        if self
            .pill
            .as_ref()
            .is_some_and(|pill| pill.layer.wl_surface() == surface)
        {
            self.draw_pill();
        } else {
            self.draw();
        }
    }

    pub(super) fn surface_mut(&mut self, surface: &wl_surface::WlSurface) -> Option<&mut Overlay> {
        [self.overlay.as_mut(), self.pill.as_mut()]
            .into_iter()
            .flatten()
            .find(|overlay| overlay.layer.wl_surface() == surface)
    }
}

pub(super) fn render(pool: &mut SlotPool, overlay: &Overlay, paint: impl FnOnce(&mut Canvas, f32)) {
    if !overlay.configured || overlay.width == 0 || overlay.height == 0 {
        return;
    }
    let scale = overlay.scale();
    let width = (f64::from(overlay.width) * scale).round() as u32;
    let height = (f64::from(overlay.height) * scale).round() as u32;
    let (buffer, pixels) = match pool.create_buffer(
        width as i32,
        height as i32,
        width as i32 * 4,
        wl_shm::Format::Argb8888,
    ) {
        Ok(pair) => pair,
        Err(err) => {
            eprintln!("[vinowhisper-tray] no buffer to draw into: {err}");
            return;
        }
    };
    let mut canvas = Canvas::new(pixels, width, height);
    paint(&mut canvas, scale as f32);

    let surface = overlay.layer.wl_surface();
    match &overlay.viewport {
        Some(viewport) => viewport.set_destination(overlay.width as i32, overlay.height as i32),
        None => surface.set_buffer_scale(overlay.buffer_scale),
    }
    surface.damage_buffer(0, 0, width as i32, height as i32);
    if let Err(err) = buffer.attach_to(surface) {
        eprintln!("[vinowhisper-tray] could not attach the frame: {err}");
        return;
    }
    overlay.layer.commit();
}

pub(super) fn place_pill(layer: &LayerSurface, settings: &Settings, captions_visible: bool) {
    let captions = if captions_visible {
        paint::logical_height(settings.size) as i32 + PILL_GAP
    } else {
        0
    };
    let (anchor, (top, bottom)) = match settings.position {
        Position::Bottom => (Anchor::BOTTOM, (0, EDGE_MARGIN + captions)),
        Position::Top => (Anchor::TOP, (EDGE_MARGIN + captions, 0)),
    };
    layer.set_anchor(anchor);
    layer.set_size(paint::PILL_WIDTH, paint::PILL_HEIGHT);
    layer.set_margin(top, 0, bottom, 0);
}

pub(super) fn place(layer: &LayerSurface, settings: &Settings) {
    let (anchor, (top, bottom)) = match settings.position {
        Position::Bottom => (Anchor::BOTTOM, (0, EDGE_MARGIN)),
        Position::Top => (Anchor::TOP, (EDGE_MARGIN, 0)),
    };
    // Full width on purpose: click-through, and no output size is needed up front.
    layer.set_anchor(anchor | Anchor::LEFT | Anchor::RIGHT);
    layer.set_size(0, paint::logical_height(settings.size));
    layer.set_margin(top, SIDE_MARGIN, bottom, SIDE_MARGIN);
}
