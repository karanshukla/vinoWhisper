//! vinowhisper-gui: a floating caption overlay, a tray icon and a global
//! shortcut for vinoWhisper, on Wayland.
//!
//! A renderer, not a second implementation. It runs `vinowhisper-caption
//! --json` and draws the events that come back, so capture, stitching and the
//! server all stay in the Python package, and the Python package gains no GUI
//! dependencies. docs/gui.md has the reasoning behind each piece.

mod app;
mod captions;
mod icon;
mod install;
mod ipc;
mod paint;
mod protocol;
mod raster;
mod session;
mod settings;
mod shortcut;
mod tray;

use std::path::PathBuf;
use std::process::ExitCode;

use ipc::Request;
use settings::Source;

/// The desktop file's name, and the app id the portal ties the global
/// shortcut to. Changing it orphans every shortcut already bound.
pub const APP_ID: &str = "io.github.karanshukla.vinowhisper";

const USAGE: &str = "\
Usage: vinowhisper-gui [OPTIONS] [COMMAND]

A floating caption overlay and tray icon for vinoWhisper. Runs
vinowhisper-caption --json and draws what it says.

Commands go to the running instance, starting one if there is none:
  show        show the overlay and start captioning (the default)
  hide        hide the overlay and stop captioning
  toggle      one or the other: bind this to a key if your desktop has
              no global-shortcuts portal
  quit        stop everything, tray icon included

Options:
  --hidden          start in the tray, captions off (for autostart)
  --source SOURCE   'output' (system audio, the default) or 'mic', this run only
  --caption PATH    the vinowhisper-caption to run, when it is not on PATH
  --install         add a launcher entry and icon for this binary
  --autostart       with --install, also start in the tray at login
  --uninstall       remove what --install added
  -V, --version     print the version
  -h, --help        print this help

The global shortcut is requested from the desktop portal, preferring
Meta+Alt+C. Change it from the tray menu (\"Change shortcut…\") or in your
desktop's shortcut settings, where it is listed under vinoWhisper. Tray
choices are remembered in $XDG_CONFIG_HOME/vinowhisper/gui.json.";

#[derive(Debug, Default, PartialEq)]
struct Args {
    command: Option<Request>,
    hidden: bool,
    source: Option<Source>,
    caption: Option<PathBuf>,
    install: bool,
    autostart: bool,
    uninstall: bool,
    help: bool,
    version: bool,
}

impl Args {
    fn parse(args: impl IntoIterator<Item = String>) -> Result<Args, String> {
        let mut parsed = Args::default();
        let mut args = args.into_iter();
        while let Some(arg) = args.next() {
            match arg.as_str() {
                "-h" | "--help" => parsed.help = true,
                "-V" | "--version" => parsed.version = true,
                "--hidden" => parsed.hidden = true,
                "--install" => parsed.install = true,
                "--autostart" => parsed.autostart = true,
                "--uninstall" => parsed.uninstall = true,
                "--source" => {
                    let value = args.next().ok_or("--source needs 'output' or 'mic'")?;
                    parsed.source = Some(Source::parse(&value).ok_or_else(|| {
                        format!("--source takes 'output' or 'mic', not {value:?}")
                    })?);
                }
                "--caption" => {
                    parsed.caption = Some(args.next().ok_or("--caption needs a path")?.into());
                }
                option if option.starts_with('-') => {
                    return Err(format!("unknown option {option}"));
                }
                word => {
                    if parsed.command.is_some() {
                        return Err(format!("one command at a time, got a second: {word:?}"));
                    }
                    let request = Request::parse(word).filter(|request| *request != Request::Ping);
                    parsed.command =
                        Some(request.ok_or_else(|| format!("unknown command {word:?}"))?);
                }
            }
        }
        if parsed.autostart && !parsed.install {
            return Err("--autostart goes with --install".into());
        }
        Ok(parsed)
    }
}

fn main() -> ExitCode {
    let args = match Args::parse(std::env::args().skip(1)) {
        Ok(args) => args,
        Err(message) => {
            eprintln!("vinowhisper-gui: {message}\n\n{USAGE}");
            return ExitCode::from(2);
        }
    };
    if args.help {
        println!("{USAGE}");
        return ExitCode::SUCCESS;
    }
    if args.version {
        println!("vinowhisper-gui {}", env!("CARGO_PKG_VERSION"));
        return ExitCode::SUCCESS;
    }
    if args.install || args.uninstall {
        return run_install(&args);
    }

    // `--hidden` only asks whether an instance exists, so an autostart that
    // finds one already running leaves it exactly as it is.
    let request = args.command.unwrap_or(if args.hidden {
        Request::Ping
    } else {
        Request::Show
    });
    match ipc::send(request) {
        Ok(()) => return ExitCode::SUCCESS,
        Err(ipc::SendError::NotRunning) => {}
        Err(ipc::SendError::Io(err)) => {
            eprintln!("vinowhisper-gui: could not reach the running instance: {err}");
            return ExitCode::FAILURE;
        }
    }
    if matches!(request, Request::Hide | Request::Quit) {
        eprintln!("vinowhisper-gui is not running");
        return ExitCode::SUCCESS;
    }

    let options = app::Options {
        visible: request != Request::Ping,
        source: args.source,
        caption: args.caption,
    };
    match app::run(options) {
        Ok(()) => ExitCode::SUCCESS,
        Err(message) => {
            eprintln!("vinowhisper-gui: {message}");
            ExitCode::FAILURE
        }
    }
}

fn run_install(args: &Args) -> ExitCode {
    let result = if args.uninstall {
        install::uninstall()
    } else {
        install::install(args.autostart, args.caption.as_deref())
    };
    match result {
        Ok(paths) => {
            let verb = if args.uninstall { "removed" } else { "wrote" };
            for path in paths {
                println!("{verb} {}", path.display());
            }
            if args.install && session::find_caption(args.caption.as_deref()).is_none() {
                eprintln!("note: {}", session::not_found_message());
            }
            ExitCode::SUCCESS
        }
        Err(err) => {
            eprintln!("vinowhisper-gui: {err}");
            ExitCode::FAILURE
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parse(args: &[&str]) -> Result<Args, String> {
        Args::parse(args.iter().map(|arg| arg.to_string()))
    }

    #[test]
    fn no_arguments_means_show() {
        assert_eq!(parse(&[]), Ok(Args::default()));
    }

    #[test]
    fn a_command_and_options_parse_together() {
        let args = parse(&["--source", "mic", "toggle"]).unwrap();
        assert_eq!(args.command, Some(Request::Toggle));
        assert_eq!(args.source, Some(Source::Mic));
    }

    #[test]
    fn bad_input_is_refused_with_a_reason() {
        assert!(
            parse(&["--source", "speakers"])
                .unwrap_err()
                .contains("'output' or 'mic'")
        );
        assert!(parse(&["restart"]).unwrap_err().contains("unknown command"));
        assert!(parse(&["ping"]).is_err(), "ping is internal, not a command");
        assert!(parse(&["show", "hide"]).is_err());
        assert!(parse(&["--frobnicate"]).is_err());
        assert!(parse(&["--autostart"]).is_err());
    }
}
