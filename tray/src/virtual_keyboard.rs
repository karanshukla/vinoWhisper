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
// Shift is the first modifier xkb defines, so bit 0 in any keymap.
const SHIFT_MASK: u32 = 1;

enum Step {
    Key(u16, u32),
    Modifiers(u32),
}

// Sway 1.9 applies no modifier from a virtual keyboard's key events, only from
// `modifiers` (measured 2026-10-24: a bare Insert reached the window), so both.
const SHIFT_INSERT: [Step; 6] = [
    Step::Key(KEY_LEFTSHIFT, PRESSED),
    Step::Modifiers(SHIFT_MASK),
    Step::Key(KEY_INSERT, PRESSED),
    Step::Key(KEY_INSERT, RELEASED),
    Step::Key(KEY_LEFTSHIFT, RELEASED),
    Step::Modifiers(0),
];

pub struct VirtualKeyboard {
    manager: ZwpVirtualKeyboardManagerV1,
    seat: WlSeat,
    qh: QueueHandle<App>,
    conn: Connection,
    // Made on first use: Sway hands a new keyboard's keymap to the focused window.
    keyboard: Option<ZwpVirtualKeyboardV1>,
    started: Instant,
}

impl VirtualKeyboard {
    // Main thread only; `shift_insert` can run anywhere.
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
        for step in SHIFT_INSERT {
            match step {
                Step::Key(key, state) => keyboard.key(time, key.into(), state),
                Step::Modifiers(depressed) => keyboard.modifiers(depressed, 0, 0, 0),
            }
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

// Real evdev codes (xkb adds 8), so a compositor using the user's own keymap agrees.
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

    #[test]
    fn shift_is_held_as_a_modifier_while_insert_goes_down() {
        let mut held = 0;
        for step in SHIFT_INSERT {
            match step {
                Step::Modifiers(depressed) => held = depressed,
                Step::Key(KEY_INSERT, _) => assert_eq!(held, SHIFT_MASK),
                Step::Key(..) => {}
            }
        }
        assert_eq!(held, 0, "nothing left held afterwards");
    }
}
