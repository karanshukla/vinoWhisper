//! The vinoWhisper settings and sessions windows.

mod forward;
mod model;
mod sessions;
mod sessions_window;
mod speak;
mod window;

use std::process::ExitCode;

const USAGE: &str = "\
vinowhisper-gui: the vinoWhisper settings window

Usage: vinowhisper-gui [--page shortcuts|captions|speech|cleanup|sessions|about]
       vinowhisper-gui --sessions
       vinowhisper-gui --open <bundle.vwsession>[#transcript[@MM:SS]|#summary]
       vinowhisper-gui --version

--sessions opens the saved sessions window, and --open opens it on one session.

Arguments meant for the tray (toggle, --hidden, dictate-*, quit, --install) are
passed on to vinowhisper-tray, so launchers written before the split keep working.
";

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match forward::decide(&args) {
        forward::Action::Window { page } => {
            window::run(vinowhisper_settings::SETTINGS_APP_ID, page.as_deref())
        }
        forward::Action::Sessions { open } => {
            sessions_window::run(vinowhisper_settings::SETTINGS_APP_ID, open.as_deref())
        }
        forward::Action::Help => {
            print!("{USAGE}");
            ExitCode::SUCCESS
        }
        forward::Action::Version => {
            println!("vinowhisper-gui {}", vinowhisper_settings::version::VERSION);
            ExitCode::SUCCESS
        }
        forward::Action::Tray => forward::exec_tray(&args),
    }
}
