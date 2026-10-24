use std::fs::File;
use std::io::Write;
use std::os::fd::OwnedFd;
use std::sync::Arc;
use std::time::{Duration, Instant};

use smithay_client_toolkit::reexports::client::globals::GlobalList;
use smithay_client_toolkit::reexports::client::protocol::wl_callback::{self, WlCallback};
use smithay_client_toolkit::reexports::client::protocol::wl_display::WlDisplay;
use smithay_client_toolkit::reexports::client::protocol::wl_seat::WlSeat;
use smithay_client_toolkit::reexports::client::{
    Connection, Dispatch, Proxy, QueueHandle, delegate_noop, event_created_child,
};
use smithay_client_toolkit::reexports::protocols::ext::data_control::v1::client::{
    ext_data_control_device_v1::{self, ExtDataControlDeviceV1},
    ext_data_control_manager_v1::ExtDataControlManagerV1,
    ext_data_control_offer_v1::{self, ExtDataControlOfferV1},
    ext_data_control_source_v1::{self, ExtDataControlSourceV1},
};
use smithay_client_toolkit::reexports::protocols_wlr::data_control::v1::client::{
    zwlr_data_control_device_v1::{self, ZwlrDataControlDeviceV1},
    zwlr_data_control_manager_v1::ZwlrDataControlManagerV1,
    zwlr_data_control_offer_v1::{self, ZwlrDataControlOfferV1},
    zwlr_data_control_source_v1::{self, ZwlrDataControlSourceV1},
};

use crate::app::App;

const MIME_TYPES: [&str; 5] = [
    "text/plain;charset=utf-8",
    "text/plain",
    "UTF8_STRING",
    "STRING",
    "TEXT",
];

// Klipper, and clipboard managers that copy it, keep nothing that carries this.
const PASSWORD_HINT: &str = "x-kde-passwordManagerHint";

const GRACE: Duration = Duration::from_millis(300);
const MIN: Duration = Duration::from_millis(500);
const MAX: Duration = Duration::from_secs(2);

// ext-data-control is the standard; the wlr- one it came from is all that Sway
// before 1.11, KWin before 6.3 and Hyprland before 0.49 offer. Same requests.
enum Manager {
    Ext(ExtDataControlManagerV1),
    Wlr(ZwlrDataControlManagerV1),
}

enum Device {
    Ext(ExtDataControlDeviceV1),
    Wlr(ZwlrDataControlDeviceV1),
}

#[derive(PartialEq)]
enum Source {
    Ext(ExtDataControlSourceV1),
    Wlr(ZwlrDataControlSourceV1),
}

impl Manager {
    fn data_source(&self, qh: &QueueHandle<App>, text: Arc<str>) -> Source {
        match self {
            Manager::Ext(manager) => Source::Ext(manager.create_data_source(qh, text)),
            Manager::Wlr(manager) => Source::Wlr(manager.create_data_source(qh, text)),
        }
    }
}

impl Device {
    fn set_selection(&self, source: Option<&Source>) {
        match self {
            Device::Ext(device) => device.set_selection(source.map(Source::ext)),
            Device::Wlr(device) => device.set_selection(source.map(Source::wlr)),
        }
    }

    fn set_primary_selection(&self, source: Option<&Source>) {
        match self {
            Device::Ext(device) => device.set_primary_selection(source.map(Source::ext)),
            // Version 1 has no primary selection; the clipboard alone still pastes in GUI apps.
            Device::Wlr(device) if device.version() >= 2 => {
                device.set_primary_selection(source.map(Source::wlr))
            }
            Device::Wlr(_) => {}
        }
    }

    fn has_primary(&self) -> bool {
        match self {
            Device::Ext(_) => true,
            Device::Wlr(device) => device.version() >= 2,
        }
    }
}

impl Source {
    // Only ever paired with the device of the same protocol.
    fn ext(&self) -> &ExtDataControlSourceV1 {
        match self {
            Source::Ext(source) => source,
            Source::Wlr(_) => unreachable!("an ext device was handed a wlr source"),
        }
    }

    fn wlr(&self) -> &ZwlrDataControlSourceV1 {
        match self {
            Source::Wlr(source) => source,
            Source::Ext(_) => unreachable!("a wlr device was handed an ext source"),
        }
    }

    fn offer(&self, mime: String) {
        match self {
            Source::Ext(source) => source.offer(mime),
            Source::Wlr(source) => source.offer(mime),
        }
    }

    fn destroy(&self) {
        match self {
            Source::Ext(source) => source.destroy(),
            Source::Wlr(source) => source.destroy(),
        }
    }
}

pub struct Clipboard {
    manager: Manager,
    device: Device,
    display: WlDisplay,
    ours: Vec<Source>,
    last_read: Option<Instant>,
    armed_at: Option<Instant>,
    waiting: bool,
}

// A paste before this arrives gets the old selection.
pub struct Taken;

impl Clipboard {
    pub fn bind(
        conn: &Connection,
        globals: &GlobalList,
        seat: &WlSeat,
        qh: &QueueHandle<App>,
    ) -> Result<Clipboard, String> {
        let (manager, device) = match globals.bind::<ExtDataControlManagerV1, _, _>(qh, 1..=1, ()) {
            Ok(manager) => {
                let device = Device::Ext(manager.get_data_device(seat, qh, ()));
                (Manager::Ext(manager), device)
            }
            Err(_) => {
                let manager = globals
                    .bind::<ZwlrDataControlManagerV1, _, _>(qh, 1..=2, ())
                    .map_err(|_| "this compositor offers no data-control protocol".to_owned())?;
                let device = Device::Wlr(manager.get_data_device(seat, qh, ()));
                if !device.has_primary() {
                    eprintln!(
                        "[vinowhisper-tray] wlr-data-control v1: no primary selection, so \
                         Shift+Insert pastes nothing in terminals that read it"
                    );
                }
                (Manager::Wlr(manager), device)
            }
        };
        Ok(Clipboard {
            manager,
            device,
            display: conn.display(),
            ours: Vec::new(),
            last_read: None,
            armed_at: None,
            waiting: false,
        })
    }

    pub fn set(&mut self, text: &str, qh: &QueueHandle<App>) {
        self.offer(text, qh);
        self.display.sync(qh, Taken);
    }

    pub fn restore(&mut self, text: &str, qh: &QueueHandle<App>) {
        self.offer(text, qh);
        self.waiting = true;
    }

    fn offer(&mut self, text: &str, qh: &QueueHandle<App>) {
        self.drop_sources();
        let text: Arc<str> = Arc::from(text);
        let source = |text: &Arc<str>| {
            let source = self.manager.data_source(qh, Arc::clone(text));
            for mime in MIME_TYPES.into_iter().chain([PASSWORD_HINT]) {
                source.offer(mime.to_owned());
            }
            source
        };
        let (clipboard, primary) = (source(&text), source(&text));
        self.device.set_selection(Some(&clipboard));
        self.device.set_primary_selection(Some(&primary));
        self.ours = vec![clipboard, primary];
        self.last_read = None;
        self.armed_at = None;
        self.waiting = false;
    }

    pub fn arm(&mut self, now: Instant) {
        if !self.ours.is_empty() {
            self.armed_at = Some(now);
        }
    }

    pub fn armed(&self) -> bool {
        self.armed_at.is_some()
    }

    pub fn clear_if_due(&mut self, now: Instant) -> bool {
        let Some(armed_at) = self.armed_at else {
            return true;
        };
        if !clear_due(armed_at, self.last_read, now) {
            return false;
        }
        if !self.ours.is_empty() {
            self.device.set_selection(None);
            self.device.set_primary_selection(None);
        }
        self.drop_sources();
        true
    }

    fn drop_sources(&mut self) {
        for source in self.ours.drain(..) {
            source.destroy();
        }
        self.armed_at = None;
    }

    // True for the first read of a restored text, which is when its clearing starts.
    fn read(&mut self, source: &Source, now: Instant) -> bool {
        if !self.ours.contains(source) {
            return false;
        }
        self.last_read = Some(now);
        std::mem::take(&mut self.waiting)
    }

    // Someone else copied since: theirs, not ours to clear.
    fn replaced(&mut self, source: &Source) {
        self.ours.retain(|ours| ours != source);
        if self.ours.is_empty() {
            self.armed_at = None;
        }
    }
}

fn clear_due(armed_at: Instant, last_read: Option<Instant>, now: Instant) -> bool {
    let since_armed = now.saturating_duration_since(armed_at);
    if since_armed >= MAX {
        return true;
    }
    last_read.is_some_and(|read| since_armed >= MIN && now.saturating_duration_since(read) >= GRACE)
}

delegate_noop!(App: ExtDataControlManagerV1);
delegate_noop!(App: ZwlrDataControlManagerV1);

fn send(app: &mut App, source: Source, mime_type: &str, fd: OwnedFd, text: &str) {
    let bytes = if mime_type == PASSWORD_HINT {
        b"secret".as_slice()
    } else {
        if let Some(clipboard) = app.clipboard_mut()
            && clipboard.read(&source, Instant::now())
        {
            app.clear_clipboard_after_paste();
        }
        text.as_bytes()
    };
    // Small enough to fit a pipe buffer, so this does not stall the loop.
    if let Err(err) = File::from(fd).write_all(bytes) {
        eprintln!("[vinowhisper-tray] could not hand over the dictated text: {err}");
    }
}

fn cancelled(app: &mut App, source: Source) {
    if let Some(clipboard) = app.clipboard_mut() {
        clipboard.replaced(&source);
    }
    source.destroy();
}

impl Dispatch<WlCallback, Taken> for App {
    fn event(
        app: &mut App,
        _: &WlCallback,
        event: wl_callback::Event,
        _: &Taken,
        _: &Connection,
        _: &QueueHandle<App>,
    ) {
        if let wl_callback::Event::Done { .. } = event {
            app.selection_taken();
        }
    }
}

// The two protocols' events have the same names and fields, so one body serves both.
macro_rules! data_control_dispatch {
    ($wrap:ident, $source:ty, $source_mod:ident, $device:ty, $device_mod:ident, $offer:ty, $offer_mod:ident) => {
        impl Dispatch<$source, Arc<str>> for App {
            fn event(
                app: &mut App,
                source: &$source,
                event: $source_mod::Event,
                text: &Arc<str>,
                _: &Connection,
                _: &QueueHandle<App>,
            ) {
                match event {
                    $source_mod::Event::Send { mime_type, fd } => {
                        send(app, Source::$wrap(source.clone()), &mime_type, fd, text)
                    }
                    $source_mod::Event::Cancelled => cancelled(app, Source::$wrap(source.clone())),
                    _ => {}
                }
            }
        }

        impl Dispatch<$device, ()> for App {
            fn event(
                _: &mut App,
                _: &$device,
                event: $device_mod::Event,
                _: &(),
                _: &Connection,
                _: &QueueHandle<App>,
            ) {
                // Every offer is announced for exactly one of these; nothing here reads other clipboards.
                match event {
                    $device_mod::Event::Selection { id: Some(offer) }
                    | $device_mod::Event::PrimarySelection { id: Some(offer) } => {
                        offer.destroy();
                    }
                    _ => {}
                }
            }

            event_created_child!(App, $device, [
                $device_mod::EVT_DATA_OFFER_OPCODE => ($offer, ()),
            ]);
        }

        impl Dispatch<$offer, ()> for App {
            fn event(
                _: &mut App,
                _: &$offer,
                _: $offer_mod::Event,
                _: &(),
                _: &Connection,
                _: &QueueHandle<App>,
            ) {
            }
        }
    };
}

data_control_dispatch!(
    Ext,
    ExtDataControlSourceV1,
    ext_data_control_source_v1,
    ExtDataControlDeviceV1,
    ext_data_control_device_v1,
    ExtDataControlOfferV1,
    ext_data_control_offer_v1
);
data_control_dispatch!(
    Wlr,
    ZwlrDataControlSourceV1,
    zwlr_data_control_source_v1,
    ZwlrDataControlDeviceV1,
    zwlr_data_control_device_v1,
    ZwlrDataControlOfferV1,
    zwlr_data_control_offer_v1
);

#[cfg(test)]
mod tests {
    use super::*;

    fn at(base: Instant, ms: u64) -> Instant {
        base + Duration::from_millis(ms)
    }

    #[test]
    fn it_waits_for_the_paste_to_read_the_text() {
        let t = Instant::now();
        assert!(!clear_due(t, None, at(t, 600)), "nothing has read it yet");
        assert!(
            !clear_due(t, Some(at(t, 20)), at(t, 200)),
            "too soon after the keys"
        );
        assert!(clear_due(t, Some(at(t, 20)), at(t, 520)));
        assert!(
            !clear_due(t, Some(at(t, 400)), at(t, 600)),
            "a read just now may be one of several"
        );
    }

    #[test]
    fn it_clears_anyway_once_nothing_can_still_be_pasting() {
        let t = Instant::now();
        assert!(clear_due(t, None, at(t, 2000)));
    }
}
