use std::fs::{File, OpenOptions};
use std::io::{self, Write};
use std::thread;
use std::time::Duration;

use rustix::ioctl::{IntegerSetter, NoArg, Opcode, Setter, ioctl, opcode};

const PATH: &str = "/dev/uinput";

const EV_SYN: u16 = 0x00;
const EV_KEY: u16 = 0x01;
const SYN_REPORT: u16 = 0;
const BUS_VIRTUAL: u16 = 0x06;

pub const KEY_LEFTSHIFT: u16 = 42;
pub const KEY_INSERT: u16 = 110;

#[repr(C)]
struct InputId {
    bustype: u16,
    vendor: u16,
    product: u16,
    version: u16,
}

#[repr(C)]
struct UinputSetup {
    id: InputId,
    name: [u8; 80],
    ff_effects_max: u32,
}

const UI_SET_EVBIT: Opcode = opcode::write::<i32>(b'U', 100);
const UI_SET_KEYBIT: Opcode = opcode::write::<i32>(b'U', 101);
const UI_DEV_SETUP: Opcode = opcode::write::<UinputSetup>(b'U', 3);
const UI_DEV_CREATE: Opcode = opcode::none(b'U', 1);

// The compositor needs a moment to adopt a new device before it will read keys from it.
const SETTLE: Duration = Duration::from_millis(250);
const BETWEEN: Duration = Duration::from_millis(4);

/// A virtual keyboard with two keys: no portal session, so nothing for the desktop to announce.
pub struct Keyboard {
    device: File,
}

impl Keyboard {
    pub fn open() -> io::Result<Keyboard> {
        let device = OpenOptions::new().write(true).open(PATH)?;
        let mut name = [0u8; 80];
        let label = b"vinoWhisper dictation";
        name[..label.len()].copy_from_slice(label);
        let setup = UinputSetup {
            id: InputId {
                bustype: BUS_VIRTUAL,
                vendor: 0,
                product: 0,
                version: 1,
            },
            name,
            ff_effects_max: 0,
        };
        // SAFETY: each opcode is paired with the argument type linux/uinput.h gives it.
        unsafe {
            ioctl(
                &device,
                IntegerSetter::<UI_SET_EVBIT>::new_usize(EV_KEY.into()),
            )?;
            for key in [KEY_LEFTSHIFT, KEY_INSERT] {
                ioctl(
                    &device,
                    IntegerSetter::<UI_SET_KEYBIT>::new_usize(key.into()),
                )?;
            }
            ioctl(&device, Setter::<UI_DEV_SETUP, UinputSetup>::new(setup))?;
            ioctl(&device, NoArg::<UI_DEV_CREATE>::new())?;
        }
        thread::sleep(SETTLE);
        Ok(Keyboard { device })
    }

    pub fn shift_insert(&mut self) -> io::Result<()> {
        for (key, pressed) in [
            (KEY_LEFTSHIFT, true),
            (KEY_INSERT, true),
            (KEY_INSERT, false),
            (KEY_LEFTSHIFT, false),
        ] {
            self.device
                .write_all(&event(EV_KEY, key, i32::from(pressed)))?;
            self.device.write_all(&event(EV_SYN, SYN_REPORT, 0))?;
            thread::sleep(BETWEEN);
        }
        Ok(())
    }
}

/// struct input_event on 64-bit Linux; the kernel stamps the time itself.
fn event(kind: u16, code: u16, value: i32) -> [u8; 24] {
    let mut bytes = [0u8; 24];
    bytes[16..18].copy_from_slice(&kind.to_ne_bytes());
    bytes[18..20].copy_from_slice(&code.to_ne_bytes());
    bytes[20..24].copy_from_slice(&value.to_ne_bytes());
    bytes
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_setup_struct_matches_the_kernel_abi() {
        assert_eq!(std::mem::size_of::<UinputSetup>(), 92);
        assert_eq!(UI_DEV_SETUP, 0x405c_5503);
        assert_eq!(UI_SET_EVBIT, 0x4004_5564);
        assert_eq!(UI_DEV_CREATE, 0x5501);
    }

    #[test]
    fn an_event_is_a_zero_timestamp_then_type_code_value() {
        let bytes = event(EV_KEY, KEY_INSERT, 1);
        assert!(bytes[..16].iter().all(|byte| *byte == 0));
        assert_eq!(u16::from_ne_bytes([bytes[16], bytes[17]]), EV_KEY);
        assert_eq!(u16::from_ne_bytes([bytes[18], bytes[19]]), KEY_INSERT);
        assert_eq!(
            i32::from_ne_bytes([bytes[20], bytes[21], bytes[22], bytes[23]]),
            1
        );
    }
}
