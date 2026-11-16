use super::*;

impl CompositorHandler for App {
    fn scale_factor_changed(
        &mut self,
        _: &Connection,
        _: &QueueHandle<Self>,
        surface: &wl_surface::WlSurface,
        factor: i32,
    ) {
        if let Some(overlay) = self.surface_mut(surface)
            && overlay.viewport.is_none()
        {
            overlay.buffer_scale = factor.max(1);
            self.redraw(surface);
        }
    }

    fn transform_changed(
        &mut self,
        _: &Connection,
        _: &QueueHandle<Self>,
        _: &wl_surface::WlSurface,
        _: wl_output::Transform,
    ) {
    }

    fn frame(&mut self, _: &Connection, _: &QueueHandle<Self>, _: &wl_surface::WlSurface, _: u32) {}

    fn surface_enter(
        &mut self,
        _: &Connection,
        _: &QueueHandle<Self>,
        _: &wl_surface::WlSurface,
        _: &wl_output::WlOutput,
    ) {
    }

    fn surface_leave(
        &mut self,
        _: &Connection,
        _: &QueueHandle<Self>,
        _: &wl_surface::WlSurface,
        _: &wl_output::WlOutput,
    ) {
    }
}

impl LayerShellHandler for App {
    fn closed(&mut self, _: &Connection, _: &QueueHandle<Self>, layer: &LayerSurface) {
        if self
            .overlay
            .as_ref()
            .is_some_and(|overlay| overlay.layer.wl_surface() == layer.wl_surface())
        {
            self.overlay = None;
            if self.visible {
                self.create_overlay();
            }
        } else if self
            .pill
            .as_ref()
            .is_some_and(|pill| pill.layer.wl_surface() == layer.wl_surface())
        {
            self.pill = None;
            self.show_pill();
        }
    }

    fn configure(
        &mut self,
        _: &Connection,
        _: &QueueHandle<Self>,
        layer: &LayerSurface,
        configure: LayerSurfaceConfigure,
        _serial: u32,
    ) {
        let surface = layer.wl_surface().clone();
        let is_pill = self
            .pill
            .as_ref()
            .is_some_and(|pill| pill.layer.wl_surface() == &surface);
        let (default_width, default_height) = if is_pill {
            (paint::PILL_WIDTH, paint::PILL_HEIGHT)
        } else {
            (0, paint::logical_height(self.settings.size))
        };
        let Some(overlay) = self.surface_mut(&surface) else {
            return;
        };
        let (width, height) = configure.new_size;
        overlay.width = if width == 0 { default_width } else { width };
        overlay.height = if height == 0 { default_height } else { height };
        overlay.configured = true;
        self.redraw(&surface);
    }
}

impl OutputHandler for App {
    fn output_state(&mut self) -> &mut OutputState {
        &mut self.outputs
    }

    fn new_output(&mut self, _: &Connection, _: &QueueHandle<Self>, _: wl_output::WlOutput) {}

    fn update_output(&mut self, _: &Connection, _: &QueueHandle<Self>, _: wl_output::WlOutput) {}

    fn output_destroyed(&mut self, _: &Connection, _: &QueueHandle<Self>, _: wl_output::WlOutput) {}
}

impl ShmHandler for App {
    fn shm_state(&mut self) -> &mut Shm {
        &mut self.shm
    }
}

impl ProvidesRegistryState for App {
    fn registry(&mut self) -> &mut RegistryState {
        &mut self.registry
    }

    registry_handlers![OutputState];
}

impl Dispatch<WpFractionalScaleV1, ()> for App {
    fn event(
        app: &mut App,
        proxy: &WpFractionalScaleV1,
        event: wp_fractional_scale_v1::Event,
        _: &(),
        _: &Connection,
        _: &QueueHandle<App>,
    ) {
        let wp_fractional_scale_v1::Event::PreferredScale { scale } = event else {
            return;
        };
        let surface = [app.overlay.as_mut(), app.pill.as_mut()]
            .into_iter()
            .flatten()
            .find(|overlay| overlay.fractional.as_ref() == Some(proxy))
            .map(|overlay| {
                overlay.scale120 = scale;
                overlay.layer.wl_surface().clone()
            });
        if let Some(surface) = surface {
            app.redraw(&surface);
        }
    }
}

delegate_noop!(App: WpFractionalScaleManagerV1);
delegate_noop!(App: ignore WlSeat);
delegate_noop!(App: WpViewporter);
delegate_noop!(App: ignore WpViewport);
delegate_registry!(App);
smithay_client_toolkit::delegate_dispatch2!(App);
