//! The vinoWhisper settings window. Reads and writes the same two files as the tray
//! (`gui.json`, `language.json`) through `vinowhisper-settings`.

mod forward;
mod theme;
mod window;

use std::process::ExitCode;

const USAGE: &str = "\
vinowhisper-gui: the vinoWhisper settings window

Usage: vinowhisper-gui [--page shortcuts|captions|speech]

Arguments meant for the tray (toggle, --hidden, dictate-*, quit, --install) are
passed on to vinowhisper-tray, so launchers written before the split keep working.
";

// Changing this orphans every shortcut already bound.
const APP_ID: &str = "io.github.karanshukla.vinowhisper.settings";

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match forward::decide(&args) {
        forward::Action::Window { page } => window::run(APP_ID, page.as_deref()),
        forward::Action::Help => {
            print!("{USAGE}");
            ExitCode::SUCCESS
        }
        forward::Action::Tray => forward::exec_tray(&args),
    }
}
