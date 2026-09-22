use std::path::PathBuf;
use std::time::{Duration, Instant};

use smithay_client_toolkit::compositor::{CompositorHandler, CompositorState, Region};
use smithay_client_toolkit::output::{OutputHandler, OutputState};
use smithay_client_toolkit::reexports::calloop::channel::{self, Sender};
use smithay_client_toolkit::reexports::calloop::timer::{TimeoutAction, Timer};
use smithay_client_toolkit::reexports::calloop::{EventLoop, LoopHandle, LoopSignal};
use smithay_client_toolkit::reexports::calloop_wayland_source::WaylandSource;
use smithay_client_toolkit::reexports::client::globals::registry_queue_init;
use smithay_client_toolkit::reexports::client::protocol::{
    wl_output, wl_seat::WlSeat, wl_shm, wl_surface,
};
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
use crate::clipboard::Clipboard;
use crate::dictation::{Action, Dictation};
use crate::dictator::Dictator;
use crate::install;
use crate::ipc::{self, Request};
use crate::paint::{self, Painter};
use crate::paste::Paster;
use crate::protocol::{Dictate, Event};
use crate::raster::Canvas;
use crate::session::{self, Session};
use crate::settings::{Position, Settings, Source, TextSize};
use crate::shortcut::{self, Shortcut};
use crate::tray::{self, Tray};
use crate::virtual_keyboard::VirtualKeyboard;

const SIDE_MARGIN: i32 = 16;
const EDGE_MARGIN: i32 = 48;
const PILL_GAP: i32 = 12;

const KILL_AFTER: Duration = Duration::from_secs(4);
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
    DictateKey {
        down: bool,
    },
    Dictation {
        generation: u64,
        event: Dictate,
    },
    DictatorExited {
        generation: u64,
        message: String,
    },
    Pasted(Result<(), String>),
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

struct Overlay {
    layer: LayerSurface,
    fractional: Option<WpFractionalScaleV1>,
    viewport: Option<WpViewport>,
    width: u32,
    height: u32,
    configured: bool,
    scale120: u32,
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
    pill: Option<Overlay>,
    painter: Painter,

    captions: Captions,
    settings: Settings,
    source_override: Option<Source>,
    visible: bool,
    session: Option<Session>,
    generation: u64,
    restart_pending: bool,
    quitting: bool,
    caption_program: Option<PathBuf>,

    handle: LoopHandle<'static, App>,
    ticking: bool,
    signal: LoopSignal,
    tx: Sender<Command>,
    tray: Option<tray::Handle>,
    tray_view: Option<tray::View>,
    shortcut: Shortcut,
    shortcut_state: shortcut::State,

    dictation: Dictation,
    dictator: Option<Dictator>,
    dictator_generation: u64,
    dictate_program: Option<PathBuf>,
    clipboard: Option<Clipboard>,
    paster: Paster,
    lingering: Option<u64>,
    idle_since: Option<Instant>,
    idle_epoch: u64,
    tray_passive: bool,
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

    let ipc = {
        let tx = tx.clone();
        let forward = move |request| {
            let command = match request {
                Request::Show => Command::Show,
                Request::Hide => Command::Hide,
                Request::Toggle => Command::Toggle,
                Request::Quit => Command::Quit,
                Request::Dictate => {
                    // A tap: starts hands-free, or finishes what is running.
                    let _ = tx.send(Command::DictateKey { down: true });
                    Command::DictateKey { down: false }
                }
                Request::DictatePress => Command::DictateKey { down: true },
                Request::DictateRelease => Command::DictateKey { down: false },
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

    // Before the portal: it refuses a shortcut to an app with no desktop file.
    match install::ensure_launcher() {
        Ok(Some(path)) => eprintln!(
            "[vinowhisper-gui] added a launcher at {} (the shortcut portal needs one)",
            path.display()
        ),
        Ok(None) => {}
        Err(err) => eprintln!("[vinowhisper-gui] no launcher, so no global shortcut: {err}"),
    }
    let seat = globals.bind::<WlSeat, _, _>(&qh, 1..=1, ()).ok();
    let clipboard = match &seat {
        Some(seat) => Clipboard::bind(&conn, &globals, seat, &qh),
        None => Err("this compositor offers no seat".to_owned()),
    };
    let clipboard = match clipboard {
        Ok(clipboard) => Some(clipboard),
        Err(err) => {
            eprintln!("[vinowhisper-gui] dictation cannot set the clipboard: {err}");
            None
        }
    };
    let virtual_keyboard = seat
        .as_ref()
        .and_then(|seat| VirtualKeyboard::bind(&conn, &globals, seat, &qh));
    let settings = Settings::load();
    let shortcut = Shortcut::spawn(settings.shortcut.clone(), tx.clone());
    let paster = Paster::spawn(tx.clone(), virtual_keyboard);
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
        pill: None,
        painter: Painter::new(),
        captions: Captions::new(),
        ticking: false,
        settings,
        source_override: options.source,
        visible: false,
        session: None,
        generation: 0,
        restart_pending: false,
        quitting: false,
        caption_program: session::find_caption(options.caption.as_deref()),
        dictate_program: session::find_sibling(session::DICTATE, options.caption.as_deref()),
        handle,
        signal: event_loop.get_signal(),
        tx: tx.clone(),
        tray: None,
        tray_view: None,
        shortcut,
        shortcut_state: shortcut::State::Pending,
        dictation: Dictation::new(),
        dictator: None,
        dictator_generation: 0,
        clipboard,
        paster,
        lingering: None,
        idle_since: None,
        idle_epoch: 0,
        tray_passive: false,
    };
    app.start_dictator();
    let view = app.view();
    app.tray = tray::spawn(Tray::new(tx, view.clone()));
    app.tray_view = Some(view);
    if options.visible {
        app.show();
    }
    app.track_idle();
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
            Command::DictateKey { down } => {
                let action = self.dictation.key(down, Instant::now());
                trace(format_args!(
                    "key {} -> {action:?}, now {:?}",
                    if down { "down" } else { "up" },
                    self.dictation.phase()
                ));
                self.dictate(action);
            }
            Command::Dictation { generation, event } => {
                if self.dictator_is(generation) {
                    match &event {
                        Dictate::Level { .. } => {}
                        Dictate::Dictated { text } => trace(format_args!(
                            "dictate says Dictated ({} chars)",
                            text.chars().count()
                        )),
                        event => trace(format_args!("dictate says {event:?}")),
                    }
                    let action = self.dictation.event(event);
                    self.dictate(action);
                }
            }
            Command::DictatorExited {
                generation,
                message,
            } => {
                if self.dictator_is(generation) {
                    self.dictator = None;
                    self.dictation.child_exited(message);
                    self.dictate(None);
                }
            }
            Command::Pasted(result) => {
                if result.is_ok() {
                    self.clear_clipboard_after_paste();
                }
                self.dictation.pasted(result);
                self.dictate(None);
            }
            Command::Caption { generation, event } => {
                if self.is_current(generation) {
                    let before = self.captions.frame();
                    self.captions.apply(event);
                    if self.captions.frame() != before {
                        self.draw();
                    }
                }
            }
            Command::CaptionExited {
                generation,
                success,
                status,
                stderr,
            } => self.session_ended(generation, success, &status, &stderr),
        }
        self.track_idle();
        self.sync_tray();
    }

    fn track_idle(&mut self) {
        if self.visible || self.dictation.pill().is_some() {
            self.idle_since = None;
            self.tray_passive = false;
            return;
        }
        let minutes = self.settings.tray_idle_minutes;
        if self.idle_since.is_some() || minutes == 0 {
            return;
        }
        self.idle_since = Some(Instant::now());
        self.idle_epoch += 1;
        let epoch = self.idle_epoch;
        let _ = self.handle.insert_source(
            Timer::from_duration(Duration::from_secs(minutes.saturating_mul(60))),
            move |_, _, app: &mut App| {
                if app.idle_epoch == epoch && app.idle_since.is_some() {
                    app.tray_passive = true;
                    app.sync_tray();
                }
                TimeoutAction::Drop
            },
        );
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
        self.place_pill();
    }

    fn hide(&mut self) {
        self.visible = false;
        self.overlay = None;
        self.stop_session();
        self.place_pill();
    }

    fn quit(&mut self) {
        self.quitting = true;
        self.visible = false;
        self.overlay = None;
        self.pill = None;
        self.dictator = None;
        if self.session.is_none() {
            self.signal.stop();
            return;
        }
        self.stop_session();
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

    fn is_current(&self, generation: u64) -> bool {
        self.session
            .as_ref()
            .is_some_and(|session| session.generation == generation)
    }

    fn start_session(&mut self) {
        match &self.session {
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
        self.start_ticking();
    }

    fn start_ticking(&mut self) {
        if self.ticking {
            return;
        }
        self.ticking = true;
        let _ = self
            .handle
            .insert_source(Timer::from_duration(TICK), |_, _, app: &mut App| {
                if app.overlay.is_some() && *app.captions.phase() == Phase::Starting {
                    app.draw();
                    TimeoutAction::ToDuration(TICK)
                } else {
                    app.ticking = false;
                    TimeoutAction::Drop
                }
            });
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

    fn create_overlay(&mut self) {
        self.overlay = Some(self.create_layer("vinowhisper", |layer, app| {
            place(layer, &app.settings);
        }));
    }

    fn create_layer(&self, namespace: &str, place: impl FnOnce(&LayerSurface, &App)) -> Overlay {
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
            Err(err) => eprintln!("[vinowhisper-gui] overlay will catch clicks: {err}"),
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

    fn place(&mut self) {
        if let Some(overlay) = &self.overlay {
            place(&overlay.layer, &self.settings);
            overlay.layer.commit();
        }
        self.place_pill();
    }

    fn place_pill(&mut self) {
        if let Some(pill) = &self.pill {
            place_pill(&pill.layer, &self.settings, self.visible);
            pill.layer.commit();
        }
    }

    fn dictator_is(&self, generation: u64) -> bool {
        self.dictator
            .as_ref()
            .is_some_and(|dictator| dictator.generation == generation)
    }

    fn start_dictator(&mut self) {
        if self.dictator.is_some() || self.quitting {
            return;
        }
        let Some(program) = &self.dictate_program else {
            return;
        };
        self.dictator_generation += 1;
        match Dictator::start(program, self.dictator_generation, self.tx.clone()) {
            Ok(dictator) => self.dictator = Some(dictator),
            Err(err) => eprintln!(
                "[vinowhisper-gui] could not start {}: {err}",
                program.display()
            ),
        }
    }

    fn tell_dictator(&mut self, command: &str) {
        if command == "start" {
            self.start_dictator();
        }
        let Some(dictator) = &mut self.dictator else {
            let why = if self.dictate_program.is_none() {
                format!(
                    "{} was not found; update vinoWhisper (pip install -U vinowhisper)",
                    session::DICTATE
                )
            } else {
                format!("{} is not running", session::DICTATE)
            };
            self.dictation.fail(why);
            return;
        };
        if let Err(err) = dictator.send(command) {
            self.dictator = None;
            self.dictation
                .fail(format!("lost {}: {err}", session::DICTATE));
        }
    }

    fn dictate(&mut self, action: Option<Action>) {
        match action {
            Some(Action::Start) => self.tell_dictator("start"),
            Some(Action::Stop) => self.tell_dictator("stop"),
            Some(Action::Paste(text)) => match &mut self.clipboard {
                Some(clipboard) => clipboard.set(&text, &self.qh),
                None => self
                    .dictation
                    .pasted(Err("this desktop gives no clipboard access".into())),
            },
            None => {}
        }
        self.show_pill();
    }

    pub fn clipboard_mut(&mut self) -> Option<&mut Clipboard> {
        self.clipboard.as_mut()
    }

    fn clear_clipboard_after_paste(&mut self) {
        let Some(clipboard) = &mut self.clipboard else {
            return;
        };
        clipboard.arm(Instant::now());
        if !clipboard.armed() {
            return;
        }
        let _ = self.handle.insert_source(
            Timer::from_duration(Duration::from_millis(100)),
            |_, _, app: &mut App| {
                let done = app
                    .clipboard
                    .as_mut()
                    .is_none_or(|clipboard| clipboard.clear_if_due(Instant::now()));
                if done {
                    TimeoutAction::Drop
                } else {
                    TimeoutAction::ToDuration(Duration::from_millis(100))
                }
            },
        );
    }

    pub fn selection_taken(&mut self) {
        if !self.paster.paste() {
            self.dictation
                .pasted(Err("the paste thread is gone".into()));
            self.show_pill();
        }
    }

    fn show_pill(&mut self) {
        if self.dictation.pill().is_none() {
            self.pill = None;
            return;
        }
        if self.pill.is_none() {
            self.pill = Some(self.create_layer("vinowhisper-dictation", |layer, app| {
                place_pill(layer, &app.settings, app.visible);
            }));
        }
        self.draw_pill();

        let epoch = self.dictation.epoch();
        if let Some(linger) = self.dictation.linger()
            && self.lingering != Some(epoch)
        {
            self.lingering = Some(epoch);
            let _ = self.handle.insert_source(
                Timer::from_duration(linger),
                move |_, _, app: &mut App| {
                    app.dictation.dismiss(epoch);
                    app.show_pill();
                    app.track_idle();
                    TimeoutAction::Drop
                },
            );
        }
    }

    fn draw_pill(&mut self) {
        let (Some(pill), Some(content)) = (&self.pill, self.dictation.pill()) else {
            return;
        };
        let painter = &mut self.painter;
        render(&mut self.pool, pill, |canvas, scale| {
            painter.paint_pill(canvas, scale, &content);
        });
    }

    fn draw(&mut self) {
        let Some(overlay) = &self.overlay else {
            return;
        };
        let (painter, size, captions) = (&mut self.painter, self.settings.size, &self.captions);
        render(&mut self.pool, overlay, |canvas, scale| {
            painter.paint(canvas, scale, size, captions);
        });
    }

    fn redraw(&mut self, surface: &wl_surface::WlSurface) {
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

    fn surface_mut(&mut self, surface: &wl_surface::WlSurface) -> Option<&mut Overlay> {
        [self.overlay.as_mut(), self.pill.as_mut()]
            .into_iter()
            .flatten()
            .find(|overlay| overlay.layer.wl_surface() == surface)
    }

    fn view(&self) -> tray::View {
        tray::View {
            visible: self.visible,
            source: self.source(),
            position: self.settings.position,
            size: self.settings.size,
            status: self.status_text(),
            passive: self.tray_passive,
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

/// VINOWHISPER_GUI_TRACE=1: every dictation key and state change on stderr.
fn trace(message: std::fmt::Arguments) {
    static ON: std::sync::OnceLock<bool> = std::sync::OnceLock::new();
    if *ON.get_or_init(|| std::env::var_os("VINOWHISPER_GUI_TRACE").is_some_and(|v| v == "1")) {
        eprintln!("[vinowhisper-gui] {message}");
    }
}

fn render(pool: &mut SlotPool, overlay: &Overlay, paint: impl FnOnce(&mut Canvas, f32)) {
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
            eprintln!("[vinowhisper-gui] no buffer to draw into: {err}");
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
        eprintln!("[vinowhisper-gui] could not attach the frame: {err}");
        return;
    }
    overlay.layer.commit();
}

fn place_pill(layer: &LayerSurface, settings: &Settings, captions_visible: bool) {
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

fn place(layer: &LayerSurface, settings: &Settings) {
    let (anchor, (top, bottom)) = match settings.position {
        Position::Bottom => (Anchor::BOTTOM, (0, EDGE_MARGIN)),
        Position::Top => (Anchor::TOP, (EDGE_MARGIN, 0)),
    };
    // Full width on purpose: click-through, and no output size is needed up front.
    layer.set_anchor(anchor | Anchor::LEFT | Anchor::RIGHT);
    layer.set_size(0, paint::logical_height(settings.size));
    layer.set_margin(top, SIDE_MARGIN, bottom, SIDE_MARGIN);
}

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
