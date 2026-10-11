//! Launchers and login entries written before the split name `vinowhisper-gui` with the
//! tray's arguments. They fire before the tray has migrated them, so anything given
//! arguments is handed to `vinowhisper-tray` unchanged.

use std::os::unix::process::CommandExt;
use std::path::PathBuf;
use std::process::{Command, ExitCode};

#[derive(Debug, PartialEq, Eq)]
pub enum Action {
    Window,
    Help,
    Tray,
}

pub fn decide(args: &[String]) -> Action {
    match args.first().map(String::as_str) {
        None => Action::Window,
        Some("-h" | "--help") => Action::Help,
        Some(_) => Action::Tray,
    }
}

/// Next to this binary first (the install puts them together), then `PATH`.
fn tray_command() -> PathBuf {
    std::env::current_exe()
        .ok()
        .and_then(|exe| exe.parent().map(|dir| dir.join("vinowhisper-tray")))
        .filter(|path| path.is_file())
        .unwrap_or_else(|| PathBuf::from("vinowhisper-tray"))
}

pub fn exec_tray(args: &[String]) -> ExitCode {
    let command = tray_command();
    // exec only returns on failure
    let err = Command::new(&command).args(args).exec();
    eprintln!(
        "[vinowhisper-gui] could not run {}: {err}",
        command.display()
    );
    ExitCode::FAILURE
}

#[cfg(test)]
mod tests {
    use super::*;

    fn args(list: &[&str]) -> Vec<String> {
        list.iter().map(|s| (*s).to_owned()).collect()
    }

    #[test]
    fn no_arguments_opens_the_window() {
        assert_eq!(decide(&[]), Action::Window);
    }

    #[test]
    fn the_old_tray_arguments_go_to_the_tray() {
        for old in [
            &["--hidden"][..],
            &["toggle"],
            &["dictate-toggle"],
            &["quit"],
            &["--install"],
            &["--hidden", "--source", "mic"],
        ] {
            assert_eq!(decide(&args(old)), Action::Tray, "{old:?}");
        }
    }

    #[test]
    fn help_is_answered_here() {
        assert_eq!(decide(&args(&["--help"])), Action::Help);
    }
}
