use std::fs::File;
use std::io::Write;
use std::sync::Arc;
use std::time::{Duration, Instant};

use smithay_client_toolkit::reexports::client::globals::GlobalList;
use smithay_client_toolkit::reexports::client::protocol::wl_callback::{self, WlCallback};
use smithay_client_toolkit::reexports::client::protocol::wl_display::WlDisplay;
use smithay_client_toolkit::reexports::client::protocol::wl_seat::WlSeat;
use smithay_client_toolkit::reexports::client::{
    Connection, Dispatch, QueueHandle, delegate_noop, event_created_child,
};
use smithay_client_toolkit::reexports::protocols::ext::data_control::v1::client::{
    ext_data_control_device_v1::{self, ExtDataControlDeviceV1},
    ext_data_control_manager_v1::ExtDataControlManagerV1,
    ext_data_control_offer_v1::{self, ExtDataControlOfferV1},
    ext_data_control_source_v1::{self, ExtDataControlSourceV1},
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

// Cleared once the paste has read it: a moment after the last read, never before MIN.
const GRACE: Duration = Duration::from_millis(300);
const MIN: Duration = Duration::from_millis(500);
const MAX: Duration = Duration::from_secs(2);

/// Clipboard and primary selection both: terminals paste the primary one on Shift+Insert.
pub struct Clipboard {
    manager: ExtDataControlManagerV1,
    device: ExtDataControlDeviceV1,
    display: WlDisplay,
    ours: Vec<ExtDataControlSourceV1>,
    last_read: Option<Instant>,
    armed_at: Option<Instant>,
}

/// The compositor has taken the new selection; a paste before this gets the old one.
pub struct Taken;

impl Clipboard {
    pub fn bind(
        conn: &Connection,
        globals: &GlobalList,
        qh: &QueueHandle<App>,
    ) -> Result<Clipboard, String> {
        let manager = globals
            .bind::<ExtDataControlManagerV1, _, _>(qh, 1..=1, ())
            .map_err(|_| "this compositor offers no ext-data-control".to_owned())?;
        let seat = globals
            .bind::<WlSeat, _, _>(qh, 1..=1, ())
            .map_err(|err| format!("no seat: {err}"))?;
        let device = manager.get_data_device(&seat, qh, ());
        Ok(Clipboard {
            manager,
            device,
            display: conn.display(),
            ours: Vec::new(),
            last_read: None,
            armed_at: None,
        })
    }

    pub fn set(&mut self, text: &str, qh: &QueueHandle<App>) {
        self.drop_sources();
        let text: Arc<str> = Arc::from(text);
        let source = |text: &Arc<str>| {
            let source = self.manager.create_data_source(qh, Arc::clone(text));
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
        self.display.sync(qh, Taken);
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

    fn read(&mut self, source: &ExtDataControlSourceV1, now: Instant) {
        if self.ours.contains(source) {
            self.last_read = Some(now);
        }
    }

    // Someone else copied since: theirs, not ours to clear.
    fn replaced(&mut self, source: &ExtDataControlSourceV1) {
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

impl Dispatch<ExtDataControlSourceV1, Arc<str>> for App {
    fn event(
        app: &mut App,
        source: &ExtDataControlSourceV1,
        event: ext_data_control_source_v1::Event,
        text: &Arc<str>,
        _: &Connection,
        _: &QueueHandle<App>,
    ) {
        match event {
            ext_data_control_source_v1::Event::Send { mime_type, fd } => {
                let bytes = if mime_type == PASSWORD_HINT {
                    b"secret".as_slice()
                } else {
                    if let Some(clipboard) = app.clipboard_mut() {
                        clipboard.read(source, Instant::now());
                    }
                    text.as_bytes()
                };
                // Small enough to fit a pipe buffer, so this does not stall the loop.
                if let Err(err) = File::from(fd).write_all(bytes) {
                    eprintln!("[vinowhisper-gui] could not hand over the dictated text: {err}");
                }
            }
            ext_data_control_source_v1::Event::Cancelled => {
                if let Some(clipboard) = app.clipboard_mut() {
                    clipboard.replaced(source);
                }
                source.destroy();
            }
            _ => {}
        }
    }
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

impl Dispatch<ExtDataControlDeviceV1, ()> for App {
    fn event(
        _: &mut App,
        _: &ExtDataControlDeviceV1,
        event: ext_data_control_device_v1::Event,
        _: &(),
        _: &Connection,
        _: &QueueHandle<App>,
    ) {
        // Every offer is announced for exactly one of these; nothing here reads other clipboards.
        match event {
            ext_data_control_device_v1::Event::Selection { id: Some(offer) }
            | ext_data_control_device_v1::Event::PrimarySelection { id: Some(offer) } => {
                offer.destroy();
            }
            _ => {}
        }
    }

    event_created_child!(App, ExtDataControlDeviceV1, [
        ext_data_control_device_v1::EVT_DATA_OFFER_OPCODE => (ExtDataControlOfferV1, ()),
    ]);
}

impl Dispatch<ExtDataControlOfferV1, ()> for App {
    fn event(
        _: &mut App,
        _: &ExtDataControlOfferV1,
        _: ext_data_control_offer_v1::Event,
        _: &(),
        _: &Connection,
        _: &QueueHandle<App>,
    ) {
    }
}

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
