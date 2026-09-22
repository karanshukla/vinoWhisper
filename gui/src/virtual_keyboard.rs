use std::fs::File;
use std::io::{self, Write};
use std::os::fd::AsFd;
use std::time::Instant;

use rustix::fs::{MemfdFlags, memfd_create};
use smithay_client_toolkit::reexports::client::globals::GlobalList;
use smithay_client_toolkit::reexports::client::protocol::wl_seat::WlSeat;
use smithay_client_toolkit::reexports::client::{Connection, QueueHandle, delegate_noop};
use smithay_client_toolkit::reexports::protocols_misc::zwp_virtual_keyboard_v1::client::{
    zwp_virtual_keyboard_manager_v1::ZwpVirtualKeyboardManagerV1,
    zwp_virtual_keyboard_v1::ZwpVirtualKeyboardV1,
};

use crate::app::App;
use crate::uinput::{KEY_INSERT, KEY_LEFTSHIFT};

// wl_keyboard.keymap_format.xkb_v1 and wl_keyboard.key_state.
const XKB_V1: u32 = 1;
const PRESSED: u32 = 1;
const RELEASED: u32 = 0;

/// Shift+Insert through `zwp_virtual_keyboard_v1`: the compositor's own way of taking keys
/// from a client, so no udev rule (uinput) and no permission dialog or notification (portal).
/// wlroots compositors, niri, COSMIC and KWin offer it; GNOME does not.
pub struct VirtualKeyboard {
    manager: ZwpVirtualKeyboardManagerV1,
    seat: WlSeat,
    qh: QueueHandle<App>,
    conn: Connection,
    // Made on the first paste that needs it: a new keyboard on the seat is not free of
    // side effects (Sway hands its keymap to the focused window), so not on every start.
    keyboard: Option<ZwpVirtualKeyboardV1>,
    started: Instant,
}

impl VirtualKeyboard {
    /// None when the compositor has no such global. Main thread; `shift_insert` can run anywhere.
    pub fn bind(
        conn: &Connection,
        globals: &GlobalList,
        seat: &WlSeat,
        qh: &QueueHandle<App>,
    ) -> Option<VirtualKeyboard> {
        let manager = globals
            .bind::<ZwpVirtualKeyboardManagerV1, _, _>(qh, 1..=1, ())
            .ok()?;
        Some(VirtualKeyboard {
            manager,
            seat: seat.clone(),
            qh: qh.clone(),
            conn: conn.clone(),
            keyboard: None,
            started: Instant::now(),
        })
    }

    pub fn shift_insert(&mut self) -> io::Result<()> {
        let keyboard = match &self.keyboard {
            Some(keyboard) => keyboard,
            None => {
                let keyboard = self
                    .manager
                    .create_virtual_keyboard(&self.seat, &self.qh, ());
                if let Err(err) = upload(&keyboard, &keymap()) {
                    keyboard.destroy();
                    return Err(err);
                }
                self.keyboard.insert(keyboard)
            }
        };
        // Requests are ordered on the connection, so no settling and no gaps between keys.
        let time = u32::try_from(self.started.elapsed().as_millis()).unwrap_or(u32::MAX);
        for (key, state) in [
            (KEY_LEFTSHIFT, PRESSED),
            (KEY_INSERT, PRESSED),
            (KEY_INSERT, RELEASED),
            (KEY_LEFTSHIFT, RELEASED),
        ] {
            keyboard.key(time, key.into(), state);
        }
        self.conn.flush().map_err(io::Error::other)
    }
}

fn upload(keyboard: &ZwpVirtualKeyboardV1, text: &str) -> io::Result<()> {
    let mut file = File::from(memfd_create("vinowhisper-keymap", MemfdFlags::CLOEXEC)?);
    // With its NUL: libxkbcommon takes the keymap as a string.
    let bytes = [text.as_bytes(), &[0]].concat();
    file.write_all(&bytes)?;
    let size = u32::try_from(bytes.len()).map_err(io::Error::other)?;
    keyboard.keymap(XKB_V1, file.as_fd(), size);
    Ok(())
}

/// Two keys, at the evdev codes a physical keyboard uses (xkb adds 8). A compositor that
/// reads a virtual keyboard through the user's own keymap instead still sees Shift and Insert.
fn keymap() -> String {
    format!(
        "xkb_keymap {{\n\
         \txkb_keycodes \"vinowhisper\" {{\n\
         \t\tminimum = 8;\n\
         \t\tmaximum = 255;\n\
         \t\t<LFSH> = {shift};\n\
         \t\t<INS> = {insert};\n\
         \t}};\n\
         \txkb_types \"vinowhisper\" {{ include \"complete\" }};\n\
         \txkb_compat \"vinowhisper\" {{ include \"complete\" }};\n\
         \txkb_symbols \"vinowhisper\" {{\n\
         \t\tkey <LFSH> {{ [ Shift_L ] }};\n\
         \t\tkey <INS> {{ [ Insert ] }};\n\
         \t\tmodifier_map Shift {{ <LFSH> }};\n\
         \t}};\n\
         }};\n",
        shift = KEY_LEFTSHIFT + 8,
        insert = KEY_INSERT + 8,
    )
}

delegate_noop!(App: ZwpVirtualKeyboardManagerV1);
delegate_noop!(App: ZwpVirtualKeyboardV1);

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_keymap_puts_shift_and_insert_at_their_evdev_codes() {
        let text = keymap();
        assert!(text.contains("<LFSH> = 50;"), "{text}");
        assert!(text.contains("<INS> = 118;"), "{text}");
        assert!(text.contains("modifier_map Shift { <LFSH> };"), "{text}");
    }
}
