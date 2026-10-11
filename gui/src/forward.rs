//! Launchers and login entries written before the split name `vinowhisper-gui` with the
//! tray's arguments. They fire before the tray has migrated them, so anything given
//! arguments is handed to `vinowhisper-tray` unchanged.

use std::os::unix::process::CommandExt;
use std::path::PathBuf;
use std::process::{Command, ExitCode, Stdio};

#[derive(Debug, PartialEq, Eq)]
pub enum Action {
    /// `page` is the name given to `--page`, for the tray to deep-link into a section.
    Window {
        page: Option<String>,
    },
    Help,
    Tray,
}

pub fn decide(args: &[String]) -> Action {
    match args {
        [] => Action::Window { page: None },
        [flag, page] if flag == "--page" => Action::Window {
            page: Some(page.clone()),
        },
        [flag, ..] if flag == "-h" || flag == "--help" => Action::Help,
        _ => Action::Tray,
    }
}

/// Next to this binary first (the install puts them together), then `PATH`.
pub fn tray_command() -> PathBuf {
    std::env::current_exe()
        .ok()
        .and_then(|exe| exe.parent().map(|dir| dir.join("vinowhisper-tray")))
        .filter(|path| path.is_file())
        .unwrap_or_else(|| PathBuf::from("vinowhisper-tray"))
}

/// Tell a running tray to re-read the settings files. It prints and exits 0 when none is
/// running, which is fine: it reads them when it starts. Waited on off the UI thread, so a
/// save never stalls the window; `refused` hears the tray's reason when it says no.
pub fn reload_tray(refused: impl FnOnce(String) + Send + 'static) {
    let spawned = Command::new(tray_command())
        .arg("reload")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .spawn();
    match spawned {
        Ok(child) => {
            std::thread::spawn(move || {
                if let Ok(output) = child.wait_with_output()
                    && !output.status.success()
                {
                    let reason = String::from_utf8_lossy(&output.stderr);
                    refused(refusal(&reason));
                }
            });
        }
        Err(err) => eprintln!("[vinowhisper-gui] could not tell the tray to reload: {err}"),
    }
}

fn refusal(stderr: &str) -> String {
    let reason = stderr.trim();
    reason
        .strip_prefix("vinowhisper-tray: ")
        .unwrap_or(reason)
        .to_owned()
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Shortcuts {
    Asking,
    /// No tray running, so nothing is bound right now.
    Stopped,
    /// The tray is waiting on the desktop's portal.
    Pending,
    Bound {
        captions: String,
        dictate: String,
        configurable: bool,
    },
    Unavailable,
    /// The tray could not be run or answered something unreadable.
    Unknown(String),
}

pub fn query_shortcuts() -> Shortcuts {
    match Command::new(tray_command())
        .arg("shortcuts")
        .stdin(Stdio::null())
        .stderr(Stdio::null())
        .output()
    {
        Ok(output) if output.status.success() => {
            parse_shortcuts(&String::from_utf8_lossy(&output.stdout))
        }
        Ok(_) => Shortcuts::Unknown("the tray did not answer".into()),
        Err(err) => Shortcuts::Unknown(format!("could not run vinowhisper-tray: {err}")),
    }
}

fn parse_shortcuts(answer: &str) -> Shortcuts {
    let Ok(value) = serde_json::from_str::<serde_json::Value>(answer.trim()) else {
        return Shortcuts::Unknown("the tray answered something unreadable".into());
    };
    let text = |key: &str| value[key].as_str().unwrap_or_default().to_owned();
    match value["state"].as_str() {
        Some("stopped") => Shortcuts::Stopped,
        Some("pending") => Shortcuts::Pending,
        Some("unavailable") => Shortcuts::Unavailable,
        Some("bound") => Shortcuts::Bound {
            captions: text("captions"),
            dictate: text("dictate"),
            configurable: value["configurable"].as_bool().unwrap_or(false),
        },
        _ => Shortcuts::Unknown("the tray answered something unreadable".into()),
    }
}

/// Fire and forget: the desktop's dialog takes it from here.
pub fn run_tray(args: &[&str]) {
    match Command::new(tray_command())
        .args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
    {
        Ok(mut child) => {
            std::thread::spawn(move || {
                let _ = child.wait();
            });
        }
        Err(err) => eprintln!("[vinowhisper-gui] could not run vinowhisper-tray: {err}"),
    }
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
        assert_eq!(decide(&[]), Action::Window { page: None });
    }

    #[test]
    fn a_page_opens_the_window_on_it() {
        assert_eq!(
            decide(&args(&["--page", "speech"])),
            Action::Window {
                page: Some("speech".into())
            }
        );
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
    fn the_trays_answer_names_the_bound_keys() {
        assert_eq!(
            parse_shortcuts(
                r#"{"state":"bound","captions":"Meta+Alt+C","dictate":"Meta+H","configurable":true}"#
            ),
            Shortcuts::Bound {
                captions: "Meta+Alt+C".into(),
                dictate: "Meta+H".into(),
                configurable: true,
            }
        );
        assert_eq!(
            parse_shortcuts(r#"{"state":"stopped"}"#),
            Shortcuts::Stopped
        );
        assert_eq!(
            parse_shortcuts(r#"{"state":"unavailable","reason":"x"}"#),
            Shortcuts::Unavailable
        );
        assert!(matches!(parse_shortcuts("ok"), Shortcuts::Unknown(_)));
    }

    #[test]
    fn a_refusal_reads_without_the_program_name() {
        assert_eq!(
            refusal("vinowhisper-tray: Finish dictating before changing the language\n"),
            "Finish dictating before changing the language"
        );
    }

    #[test]
    fn help_is_answered_here() {
        assert_eq!(decide(&args(&["--help"])), Action::Help);
    }
}
