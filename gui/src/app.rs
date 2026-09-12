//! The event loop and everything that happens on it.
//!
//! One thread owns all of the state. The tray, the portal, the IPC socket and
//! the caption process each run on their own thread and reach this one only
//! by sending a Command, so nothing here is shared or locked.
//!
//! The overlay is a wlr-layer-shell surface on the *overlay* layer, and that
//! is the whole answer to floating above other applications: the overlay
//! layer sits above ordinary windows and fullscreen ones alike, which is
//! where a video being captioned usually is, with no window rule or
//! keep-above hint involved. Its input region is empty, so a click anywhere
//! on it lands on whatever is underneath. It is driven from the tray and the
//! shortcut, never by clicking it.

use std::path::PathBuf;
use std::time::Duration;

use smithay_client_toolkit::compositor::{CompositorHandler, CompositorState, Region};
use smithay_client_toolkit::output::{OutputHandler, OutputState};
use smithay_client_toolkit::reexports::calloop::channel::{self, Sender};
use smithay_client_toolkit::reexports::calloop::timer::{TimeoutAction, Timer};
use smithay_client_toolkit::reexports::calloop::{EventLoop, LoopHandle, LoopSignal};
use smithay_client_toolkit::reexports::calloop_wayland_source::WaylandSource;
use smithay_client_toolkit::reexports::client::globals::registry_queue_init;
use smithay_client_toolkit::reexports::client::protocol::{wl_output, wl_shm, wl_surface};
use smithay_client_toolkit::reexports::client::{Connection, Dispatch, QueueHandle, delegate_noop};
use smithay_client_toolkit::reexports::protocols::wp::fractional_scale::v1::client::{
    wp_fractional_scale_manager_v1::WpFractionalScaleManagerV1,
    wp_fractional_scale_v1::{self, WpFractionalScaleV1},
};
use smithay_client_toolkit::reexports::protocols::wp::viewporter::client::{
    wp_viewport::WpViewport, wp_viewporter::WpViewporter,
};
use smithay_client_toolkit::registry::{ProvidesRegistryState, RegistryState};
use smithay_client_toolkit::shell::WaylandSurface;
use smithay_client_toolkit::shell::wlr_layer::{
    Anchor, KeyboardInteractivity, Layer, LayerShell, LayerShellHandler, LayerSurface,
    LayerSurfaceConfigure,
};
use smithay_client_toolkit::shm::slot::SlotPool;
use smithay_client_toolkit::shm::{Shm, ShmHandler};
use smithay_client_toolkit::{delegate_registry, registry_handlers};

use crate::captions::{Captions, Phase};
use crate::install;
use crate::ipc::{self, Request};
use crate::paint::{self, Painter};
use crate::protocol::Event;
use crate::raster::Canvas;
use crate::session::{self, Session};
use crate::settings::{Position, Settings, Source, TextSize};
use crate::shortcut::{self, Shortcut};
use crate::tray::{self, Tray};

/// Logical pixels between the overlay and the sides of the screen.
const SIDE_MARGIN: i32 = 16;
/// Logical pixels between the overlay and the edge it is anchored to.
const EDGE_MARGIN: i32 = 48;

/// How long a caption process gets to act on SIGINT before it is killed. The
/// Python side needs a moment to stop pw-record and flush its last words.
const KILL_AFTER: Duration = Duration::from_secs(4);
/// And how long quitting waits for that, past the kill.
const QUIT_AFTER: Duration = Duration::from_secs(5);

const TICK: Duration = Duration::from_secs(1);

#[derive(Debug)]
pub enum Command {
    Show,
    Hide,
    Toggle,
    Quit,
    SetSource(Source),
    SetPosition(Position),
    SetSize(TextSize),
    ConfigureShortcut,
    Shortcut(shortcut::State),
    Caption {
        generation: u64,
        event: Event,
    },
    CaptionExited {
        generation: u64,
        success: bool,
        status: String,
        stderr: Vec<String>,
    },
}

pub struct Options {
    pub visible: bool,
    pub source: Option<Source>,
    pub caption: Option<PathBuf>,
}

/// The layer surface while it is showing. Dropped to hide it.
struct Overlay {
    layer: LayerSurface,
    fractional: Option<WpFractionalScaleV1>,
    viewport: Option<WpViewport>,
    /// Logical size, as configured by the compositor.
    width: u32,
    height: u32,
    configured: bool,
    /// The compositor's preferred scale in 120ths (wp_fractional_scale_v1).
    scale120: u32,
    /// The whole-number fallback when fractional scaling is not offered.
    buffer_scale: i32,
}

impl Overlay {
    fn scale(&self) -> f64 {
        if self.viewport.is_some() {
            f64::from(self.scale120) / 120.0
        } else {
            f64::from(self.buffer_scale)
        }
    }
}

impl Drop for Overlay {
    fn drop(&mut self) {
        // Before the surface they belong to, which the layer drops after this.
        if let Some(viewport) = self.viewport.take() {
            viewport.destroy();
        }
        if let Some(fractional) = self.fractional.take() {
            fractional.destroy();
        }
    }
}

pub struct App {
    registry: RegistryState,
    outputs: OutputState,
    compositor: CompositorState,
    layer_shell: LayerShell,
    shm: Shm,
    pool: SlotPool,
    scaling: Option<(WpFractionalScaleManagerV1, WpViewporter)>,
    qh: QueueHandle<App>,
    overlay: Option<Overlay>,
    painter: Painter,

    captions: Captions,
    settings: Settings,
    /// `--source` for this run, until the tray picks one.
    source_override: Option<Source>,
    visible: bool,
    session: Option<Session>,
    generation: u64,
    restart_pending: bool,
    quitting: bool,
    caption_program: Option<PathBuf>,

    handle: LoopHandle<'static, App>,
    signal: LoopSignal,
    tx: Sender<Command>,
    tray: Option<tray::Handle>,
    tray_view: Option<tray::View>,
    shortcut: Shortcut,
    shortcut_state: shortcut::State,
}

pub fn run(options: Options) -> Result<(), String> {
    let conn = Connection::connect_to_env().map_err(|err| {
        format!(
            "no Wayland session to draw on ({err}). The overlay is Wayland-only; \
             vinowhisper-caption works in any terminal."
        )
    })?;
    let (globals, queue) =
        registry_queue_init::<App>(&conn).map_err(|err| format!("Wayland registry: {err}"))?;
    let qh = queue.handle();
    let compositor =
        CompositorState::bind(&globals, &qh).map_err(|err| format!("wl_compositor: {err}"))?;
    let layer_shell = LayerShell::bind(&globals, &qh).map_err(|_| {
        "this compositor does not offer wlr-layer-shell, which is what lets the overlay \
         float above other windows. KDE Plasma, Sway, Hyprland, niri and COSMIC have it; \
         GNOME does not. vinowhisper-caption in a terminal works everywhere."
            .to_owned()
    })?;
    let shm = Shm::bind(&globals, &qh).map_err(|err| format!("wl_shm: {err}"))?;
    // Fractional scaling takes both: the compositor names the scale, and the
    // viewport maps a buffer of that many pixels onto the logical size.
    // Without them this renders at the next whole-number scale and lets the
    // compositor shrink it, which a 1.5x display shows as slightly soft text.
    let scaling = match (
        globals.bind::<WpFractionalScaleManagerV1, _, _>(&qh, 1..=1, ()),
        globals.bind::<WpViewporter, _, _>(&qh, 1..=1, ()),
    ) {
        (Ok(manager), Ok(viewporter)) => Some((manager, viewporter)),
        _ => None,
    };
    let pool = SlotPool::new(1 << 20, &shm).map_err(|err| format!("shared memory: {err}"))?;

    let mut event_loop = EventLoop::<App>::try_new().map_err(|err| format!("event loop: {err}"))?;
    let handle = event_loop.handle();
    WaylandSource::new(conn.clone(), queue)
        .insert(handle.clone())
        .map_err(|err| format!("Wayland event source: {err}"))?;
    let (tx, commands) = channel::channel();
    handle
        .insert_source(commands, |event, _, app: &mut App| {
            if let channel::Event::Msg(command) = event {
                app.handle(command);
            }
        })
        .map_err(|err| format!("command channel: {err}"))?;
    handle
        .insert_source(Timer::from_duration(TICK), |_, _, app: &mut App| {
            app.tick();
            TimeoutAction::ToDuration(TICK)
        })
        .map_err(|err| format!("timer: {err}"))?;

    let ipc = {
        let tx = tx.clone();
        let forward = move |request| {
            let command = match request {
                Request::Show => Command::Show,
                Request::Hide => Command::Hide,
                Request::Toggle => Command::Toggle,
                Request::Quit => Command::Quit,
                Request::Ping => return,
            };
            let _ = tx.send(command);
        };
        match ipc::listen(forward) {
            Ok(listener) => Some(listener),
            Err(err) if err.kind() == std::io::ErrorKind::AddrInUse => return Err(err.to_string()),
            Err(err) => {
                eprintln!(
                    "[vinowhisper-gui] `vinowhisper-gui toggle` will not reach this instance: {err}"
                );
                None
            }
        }
    };

    // Before the portal is asked for anything: it will not grant a shortcut
    // to an app with no desktop file (see install::ensure_launcher).
    match install::ensure_launcher() {
        Ok(Some(path)) => eprintln!(
            "[vinowhisper-gui] added a launcher at {} (the shortcut portal needs one)",
            path.display()
        ),
        Ok(None) => {}
        Err(err) => eprintln!("[vinowhisper-gui] no launcher, so no global shortcut: {err}"),
    }
    let settings = Settings::load();
    let shortcut = Shortcut::spawn(settings.shortcut.clone(), tx.clone());
    let mut app = App {
        registry: RegistryState::new(&globals),
        outputs: OutputState::new(&globals, &qh),
        compositor,
        layer_shell,
        shm,
        pool,
        scaling,
        qh,
        overlay: None,
        painter: Painter::new(),
        captions: Captions::new(),
        settings,
        source_override: options.source,
        visible: false,
        session: None,
        generation: 0,
        restart_pending: false,
        quitting: false,
        caption_program: session::find_caption(options.caption.as_deref()),
        handle,
        signal: event_loop.get_signal(),
        tx: tx.clone(),
        tray: None,
        tray_view: None,
        shortcut,
        shortcut_state: shortcut::State::Pending,
    };
    let view = app.view();
    app.tray = tray::spawn(Tray::new(tx, view.clone()));
    app.tray_view = Some(view);
    if options.visible {
        app.show();
    }
    app.sync_tray();

    event_loop
        .run(None, &mut app, |_| {})
        .map_err(|err| format!("event loop: {err}"))?;

    if let Some(tray) = app.tray.take() {
        tray.shutdown().wait();
    }
    drop(ipc);
    Ok(())
}

impl App {
    fn handle(&mut self, command: Command) {
        match command {
            Command::Show => self.show(),
            Command::Hide => self.hide(),
            Command::Toggle if self.visible => self.hide(),
            Command::Toggle => self.show(),
            Command::Quit => self.quit(),
            Command::SetSource(source) => self.set_source(source),
            Command::SetPosition(position) => {
                self.settings.position = position;
                self.settings.save();
                self.place();
            }
            Command::SetSize(size) => {
                self.settings.size = size;
                self.settings.save();
                self.place();
            }
            Command::ConfigureShortcut => self.shortcut.configure(),
            Command::Shortcut(state) => self.shortcut_state = state,
            Command::Caption { generation, event } => {
                if self.is_current(generation) {
                    self.captions.apply(event);
                    self.draw();
                }
            }
            Command::CaptionExited {
                generation,
                success,
                status,
                stderr,
            } => self.session_ended(generation, success, &status, &stderr),
        }
        self.sync_tray();
    }

    fn show(&mut self) {
        if self.quitting {
            return;
        }
        self.visible = true;
        if self.overlay.is_none() {
            self.create_overlay();
        }
        self.start_session();
    }

    /// Hidden means stopped, not merely invisible. An overlay that kept
    /// transcribing out of sight would keep the server from ever idling out,
    /// and scale-to-zero is the reason the server is socket-activated at all
    /// (docs/architecture.md).
    fn hide(&mut self) {
        self.visible = false;
        self.overlay = None;
        self.stop_session();
    }

    fn quit(&mut self) {
        self.quitting = true;
        self.visible = false;
        self.overlay = None;
        if self.session.is_none() {
            self.signal.stop();
            return;
        }
        self.stop_session();
        // The session reporting its exit is the clean way out (see
        // session_ended); this is for one that never does.
        let _ = self
            .handle
            .insert_source(Timer::from_duration(QUIT_AFTER), |_, _, app| {
                app.signal.stop();
                TimeoutAction::Drop
            });
    }

    fn source(&self) -> Source {
        self.source_override.unwrap_or(self.settings.source)
    }

    fn set_source(&mut self, source: Source) {
        let changed = source != self.source();
        self.source_override = None;
        if self.settings.source != source {
            self.settings.source = source;
            self.settings.save();
        }
        if changed {
            self.restart_session();
        }
    }

    // --- the caption process -------------------------------------------

    fn is_current(&self, generation: u64) -> bool {
        self.session
            .as_ref()
            .is_some_and(|session| session.generation == generation)
    }

    fn start_session(&mut self) {
        match &self.session {
            // Still shutting down the last one; start again once it has gone.
            Some(session) if session.stopping() => {
                self.restart_pending = true;
                return;
            }
            Some(_) => return,
            None => {}
        }
        self.generation += 1;
        self.captions.reset();
        match &self.caption_program {
            None => self.captions.fail(session::not_found_message()),
            Some(program) => {
                match Session::start(program, self.source(), self.generation, self.tx.clone()) {
                    Ok(session) => self.session = Some(session),
                    Err(err) => self
                        .captions
                        .fail(format!("Could not start {}: {err}", program.display())),
                }
            }
        }
        self.draw();
    }

    fn stop_session(&mut self) {
        self.restart_pending = false;
        let Some(session) = &mut self.session else {
            return;
        };
        if session.stopping() {
            return;
        }
        session.interrupt();
        let generation = session.generation;
        let _ = self
            .handle
            .insert_source(Timer::from_duration(KILL_AFTER), move |_, _, app| {
                if let Some(session) = &app.session
                    && session.generation == generation
                {
                    eprintln!("[vinowhisper-gui] vinowhisper-caption ignored SIGINT; killing it");
                    session.kill();
                }
                TimeoutAction::Drop
            });
    }

    fn restart_session(&mut self) {
        if self.session.is_some() {
            self.stop_session();
            self.restart_pending = self.visible;
        } else if self.visible {
            self.start_session();
        }
    }

    fn session_ended(&mut self, generation: u64, success: bool, status: &str, stderr: &[String]) {
        if !self.is_current(generation) {
            return;
        }
        let asked_to_stop = self
            .session
            .take()
            .is_some_and(|session| session.stopping());
        if !asked_to_stop && !success && !matches!(self.captions.phase(), Phase::Failed(_)) {
            // It died without writing an Error record, so the best account of
            // why is the last thing it said on stderr.
            let why = stderr
                .last()
                .cloned()
                .unwrap_or_else(|| format!("vinowhisper-caption exited ({status})"));
            self.captions.fail(why);
        } else {
            self.captions.ended();
        }
        if self.quitting {
            self.signal.stop();
            return;
        }
        if std::mem::take(&mut self.restart_pending) && self.visible {
            self.start_session();
        }
        self.draw();
    }

    // --- the overlay ---------------------------------------------------

    fn create_overlay(&mut self) {
        let surface = self.compositor.create_surface(&self.qh);
        let layer = self.layer_shell.create_layer_surface(
            &self.qh,
            surface,
            Layer::Overlay,
            Some("vinowhisper"),
            None,
        );
        layer.set_keyboard_interactivity(KeyboardInteractivity::None);
        // Zero, not -1: stay clear of panels rather than sliding under them.
        layer.set_exclusive_zone(0);
        place(&layer, &self.settings);
        // An empty input region, so every click goes through to the window
        // underneath: the overlay must never block the video controls it is
        // sitting over.
        match Region::new(&self.compositor) {
            Ok(region) => layer
                .wl_surface()
                .set_input_region(Some(region.wl_region())),
            Err(err) => eprintln!("[vinowhisper-gui] overlay will catch clicks: {err}"),
        }
        let (fractional, viewport) = match &self.scaling {
            Some((manager, viewporter)) => (
                Some(manager.get_fractional_scale(layer.wl_surface(), &self.qh, ())),
                Some(viewporter.get_viewport(layer.wl_surface(), &self.qh, ())),
            ),
            None => (None, None),
        };
        // The first commit carries no buffer; the compositor answers with a
        // configure, and drawing starts from there.
        layer.commit();
        self.overlay = Some(Overlay {
            layer,
            fractional,
            viewport,
            width: 0,
            height: 0,
            configured: false,
            scale120: 120,
            buffer_scale: 1,
        });
    }

    fn place(&mut self) {
        if let Some(overlay) = &self.overlay {
            place(&overlay.layer, &self.settings);
            overlay.layer.commit();
        }
    }

    fn draw(&mut self) {
        let Some(overlay) = &self.overlay else {
            return;
        };
        if !overlay.configured || overlay.width == 0 || overlay.height == 0 {
            return;
        }
        let scale = overlay.scale();
        let width = (f64::from(overlay.width) * scale).round() as u32;
        let height = (f64::from(overlay.height) * scale).round() as u32;
        let (buffer, pixels) = match self.pool.create_buffer(
            width as i32,
            height as i32,
            width as i32 * 4,
            wl_shm::Format::Argb8888,
        ) {
            Ok(pair) => pair,
            Err(err) => {
                eprintln!("[vinowhisper-gui] no buffer to draw into: {err}");
                return;
            }
        };
        let mut canvas = Canvas::new(pixels, width, height);
        self.painter.paint(
            &mut canvas,
            scale as f32,
            self.settings.size,
            &self.captions,
        );

        let surface = overlay.layer.wl_surface();
        match &overlay.viewport {
            Some(viewport) => viewport.set_destination(overlay.width as i32, overlay.height as i32),
            None => surface.set_buffer_scale(overlay.buffer_scale),
        }
        surface.damage_buffer(0, 0, width as i32, height as i32);
        if let Err(err) = buffer.attach_to(surface) {
            eprintln!("[vinowhisper-gui] could not attach the frame: {err}");
            return;
        }
        overlay.layer.commit();
    }

    /// Once a second: only the "Starting… Ns" counter needs a clock, since
    /// everything else redraws when an event arrives.
    fn tick(&mut self) {
        if self.overlay.is_some() && *self.captions.phase() == Phase::Starting {
            self.draw();
        }
    }

    // --- the tray ------------------------------------------------------

    fn view(&self) -> tray::View {
        tray::View {
            visible: self.visible,
            source: self.source(),
            position: self.settings.position,
            size: self.settings.size,
            status: self.status_text(),
            shortcut: self.shortcut_state.clone(),
        }
    }

    fn status_text(&self) -> String {
        if !self.visible {
            return "Captions hidden".into();
        }
        match self.captions.phase() {
            Phase::Starting => "Starting…".into(),
            Phase::Live => match (self.captions.device(), self.captions.degraded()) {
                (Some(device), true) => format!("Live on {device}, not the NPU: expect more lag"),
                (Some(device), false) => format!("Live on {device}"),
                (None, _) => "Live".into(),
            },
            Phase::Stopped => "Stopped".into(),
            Phase::Failed(message) => format!("Stopped: {message}"),
        }
    }

    /// Only when something in the menu or tooltip actually changed: caption
    /// events arrive a couple of times a second, and each update is a D-Bus
    /// round trip for the tray host.
    fn sync_tray(&mut self) {
        let view = self.view();
        if self.tray_view.as_ref() == Some(&view) {
            return;
        }
        if let Some(tray) = &self.tray {
            tray::show(tray, view.clone());
        }
        self.tray_view = Some(view);
    }
}

fn place(layer: &LayerSurface, settings: &Settings) {
    let (anchor, (top, bottom)) = match settings.position {
        Position::Bottom => (Anchor::BOTTOM, (0, EDGE_MARGIN)),
        Position::Top => (Anchor::TOP, (EDGE_MARGIN, 0)),
    };
    // Full width, with the box centred inside it by the painter: the surface
    // is click-through, so the empty sides cost nothing, and this way no
    // output size has to be known before the compositor picks an output.
    layer.set_anchor(anchor | Anchor::LEFT | Anchor::RIGHT);
    layer.set_size(0, paint::logical_height(settings.size));
    layer.set_margin(top, SIDE_MARGIN, bottom, SIDE_MARGIN);
}

// --- Wayland plumbing ----------------------------------------------------

impl CompositorHandler for App {
    fn scale_factor_changed(
        &mut self,
        _: &Connection,
        _: &QueueHandle<Self>,
        surface: &wl_surface::WlSurface,
        factor: i32,
    ) {
        if let Some(overlay) = &mut self.overlay
            && overlay.layer.wl_surface() == surface
            && overlay.viewport.is_none()
        {
            overlay.buffer_scale = factor.max(1);
            self.draw();
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
    /// The compositor withdrew the surface, usually because its output went
    /// away. Put it back if it is meant to be showing.
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
        let height = paint::logical_height(self.settings.size);
        let Some(overlay) = &mut self.overlay else {
            return;
        };
        if overlay.layer.wl_surface() != layer.wl_surface() {
            return;
        }
        let (width, configured_height) = configure.new_size;
        overlay.width = width;
        overlay.height = if configured_height == 0 {
            height
        } else {
            configured_height
        };
        overlay.configured = true;
        self.draw();
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
        if let Some(overlay) = &mut app.overlay
            && overlay.fractional.as_ref() == Some(proxy)
        {
            overlay.scale120 = scale;
            app.draw();
        }
    }
}

delegate_noop!(App: WpFractionalScaleManagerV1);
delegate_noop!(App: WpViewporter);
delegate_noop!(App: ignore WpViewport);
delegate_registry!(App);
smithay_client_toolkit::delegate_dispatch2!(App);
