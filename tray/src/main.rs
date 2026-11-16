mod announce;
mod app;
mod captions;
mod dictation;
mod dictator;
mod icon;
mod idle;
mod install;
mod ipc;
mod media;
mod paint;
mod portal;
mod protocol;
mod raster;
mod restart;
mod session;
mod shortcut;
mod summaries;
mod tray;

use std::path::PathBuf;
use std::process::ExitCode;

use ipc::Request;
use vinowhisper_settings as settings;
use vinowhisper_settings::Source;

// Changing this orphans every shortcut already bound.
pub const APP_ID: &str = "io.github.karanshukla.vinowhisper";

const USAGE: &str = "\
Usage: vinowhisper-tray [OPTIONS] [COMMAND]

A floating overlay and tray icon for vinoWhisper: live captions, and
dictation into the focused window. Runs vinowhisper-caption --json and
vinowhisper-dictate --json and draws what they say.

Commands go to the running instance, starting one if there is none:
  show        show the overlay and start captioning (the default)
  hide        hide the overlay and stop captioning
  toggle      one or the other: bind this to a key if your desktop has
              no global-shortcuts portal
  dictate     start dictating from the microphone, or stop and type what
              was said: the tap half of the dictation key, for desktops
              with no global-shortcuts portal
  dictate-press, dictate-release
              the hold half: bound to a key's press and release (sway:
              bindsym, and bindsym --release), holding it talks and
              letting go types
  quit        stop everything, tray icon included
  reload      re-read the settings files, after changing them by hand or
              in vinowhisper-gui (it sends this itself). Exits 1 when a
              language change is refused because dictation is running
  shortcuts   print the bound shortcuts as JSON, for vinowhisper-gui
  configure-shortcut
              open the desktop's dialog for changing them
  summaries-ready
              notify about finished session summaries (the server sends
              this itself)
  restart     quit the running instance and start this binary in its
              place, in the tray: installers run it after replacing the
              binary. Starts nothing when none is running

Options:
  --hidden          start in the tray, captions off (for autostart)
  --source SOURCE   'output' (system audio, the default) or 'mic', this run only
  --caption PATH    the vinowhisper-caption to run, when it is not on PATH
  --install         add a launcher entry and icon for this binary
  --autostart       start in the tray at login (with --install, or alone for
                    a packaged copy whose launcher came with the package)
  --uninstall       remove what --install and --autostart added
  --export-desktop DIR
                    for packagers: write the launcher and icon under DIR
                    (a buildroot's /usr/share), naming the command, not a path
  -V, --version     print the version
  -h, --help        print this help

Two global shortcuts are requested from the desktop portal: captions,
preferring Meta+Alt+C, and dictation, preferring Meta+H (the dictation key
on laptops that have one). Hold the dictation key to talk and release to
type, or tap it to start and tap again to finish. The text is typed through
the remote-desktop portal, which asks permission once; a session stays open
between dictations and closes after typing_idle_minutes. Change either shortcut
from vinowhisper-gui (Shortcuts) or in your desktop's shortcut settings,
where they are listed under vinoWhisper. Settings are kept in
$XDG_CONFIG_HOME/vinowhisper/gui.json and language.json.";

#[derive(Debug, Default, PartialEq)]
struct Args {
    command: Option<Request>,
    restart: bool,
    hidden: bool,
    source: Option<Source>,
    caption: Option<PathBuf>,
    install: bool,
    autostart: bool,
    uninstall: bool,
    export_desktop: Option<PathBuf>,
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
                "--export-desktop" => {
                    parsed.export_desktop = Some(
                        args.next()
                            .ok_or("--export-desktop needs a directory")?
                            .into(),
                    );
                }
                option if option.starts_with('-') => {
                    return Err(format!("unknown option {option}"));
                }
                word => {
                    if parsed.command.is_some() || parsed.restart {
                        return Err(format!("one command at a time, got a second: {word:?}"));
                    }
                    if word == "restart" {
                        parsed.restart = true;
                        continue;
                    }
                    let request = Request::parse(word).filter(|request| *request != Request::Ping);
                    parsed.command =
                        Some(request.ok_or_else(|| format!("unknown command {word:?}"))?);
                }
            }
        }
        if parsed.export_desktop.is_some()
            && (parsed.install || parsed.autostart || parsed.uninstall)
        {
            return Err("--export-desktop is for packagers and goes on its own".into());
        }
        if parsed.uninstall && (parsed.install || parsed.autostart) {
            return Err("--uninstall undoes --install and --autostart; pick one".into());
        }
        Ok(parsed)
    }
}

fn main() -> ExitCode {
    let args = match Args::parse(std::env::args().skip(1)) {
        Ok(args) => args,
        Err(message) => {
            eprintln!("vinowhisper-tray: {message}\n\n{USAGE}");
            return ExitCode::from(2);
        }
    };
    if args.help {
        println!("{USAGE}");
        return ExitCode::SUCCESS;
    }
    if args.version {
        println!("vinowhisper-tray {}", env!("CARGO_PKG_VERSION"));
        return ExitCode::SUCCESS;
    }
    if args.install || args.autostart || args.uninstall || args.export_desktop.is_some() {
        return run_install(&args);
    }
    if args.restart {
        return run_restart();
    }

    let request = args.command.unwrap_or(if args.hidden {
        Request::Ping
    } else {
        Request::Show
    });
    match ipc::send(request) {
        Ok(answer) => {
            if !answer.is_empty() {
                println!("{answer}");
            }
            return ExitCode::SUCCESS;
        }
        Err(ipc::SendError::Refused(reason)) => {
            eprintln!("vinowhisper-tray: {reason}");
            return ExitCode::FAILURE;
        }
        Err(ipc::SendError::NotRunning) => {}
        Err(ipc::SendError::Io(err)) => {
            eprintln!("vinowhisper-tray: could not reach the running instance: {err}");
            return ExitCode::FAILURE;
        }
    }
    if request == Request::Shortcuts {
        // An answer, not an error: the settings window shows it as "not running".
        println!("{{\"state\":\"stopped\"}}");
        return ExitCode::SUCCESS;
    }
    if !starts_an_instance(request) {
        eprintln!("vinowhisper-tray is not running");
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
            eprintln!("vinowhisper-tray: {message}");
            ExitCode::FAILURE
        }
    }
}

// (pinned by `tests::only_show_toggle_and_ping_start_a_tray`, `tests::a_request_for_a_running_tray_does_not_start_one`)
fn starts_an_instance(request: Request) -> bool {
    match request {
        Request::Show | Request::Toggle | Request::Ping => true,
        Request::Shortcuts
        | Request::Hide
        | Request::Quit
        | Request::Reload
        | Request::ConfigureShortcut
        | Request::Dictate
        | Request::DictatePress
        | Request::DictateRelease
        | Request::SummariesReady => false,
    }
}

// In the Flatpak the running tray is in another sandbox: its pid and command line are out
// of sight, and the build to start is the newest installed, not the one this runs from.
fn run_restart_sandboxed() -> ExitCode {
    let gone = || matches!(ipc::send(Request::Ping), Err(ipc::SendError::NotRunning));
    if gone() {
        println!("vinowhisper-tray is not running; nothing to restart");
        return ExitCode::SUCCESS;
    }
    if let Err(err) = ipc::send(Request::Quit)
        && !matches!(err, ipc::SendError::NotRunning)
    {
        eprintln!("vinowhisper-tray: could not stop the running instance: {err:?}");
        return ExitCode::FAILURE;
    }
    if !restart::wait_until(restart::EXIT_WAIT, gone) {
        eprintln!(
            "vinowhisper-tray: the running instance has not exited after {}s; not starting \
             another",
            restart::EXIT_WAIT.as_secs()
        );
        return ExitCode::FAILURE;
    }
    let spawn = settings::sandbox::latest_instance("vinowhisper-tray");
    let args: Vec<String> = spawn
        .get_args()
        .map(|arg| arg.to_string_lossy().into_owned())
        .chain(["--hidden".to_owned()])
        .collect();
    let mut helper = match restart::spawn_detached_child(spawn.get_program().as_ref(), &args) {
        Ok(helper) => helper,
        Err(err) => {
            eprintln!("vinowhisper-tray: could not run flatpak-spawn: {err}");
            return ExitCode::FAILURE;
        }
    };
    let started = restart::wait_until(restart::START_WAIT, || ipc::send(Request::Ping).is_ok());
    // flatpak-spawn lives as long as the tray it started, and keeps this sandbox alive. Kill
    // it rather than signal it: it would pass the signal on to the tray.
    let _ = helper.kill();
    let _ = helper.wait();
    if !started {
        eprintln!("vinowhisper-tray: started the new build but it is not answering");
        return ExitCode::FAILURE;
    }
    println!("restarted vinowhisper-tray, in the tray (captions are off until shown)");
    ExitCode::SUCCESS
}

fn run_restart() -> ExitCode {
    if settings::sandbox::flatpak_id().is_some() {
        return run_restart_sandboxed();
    }
    let pid = match ipc::running_pid() {
        Ok(pid) => pid,
        Err(ipc::SendError::NotRunning) => {
            println!("vinowhisper-tray is not running; nothing to restart");
            return ExitCode::SUCCESS;
        }
        Err(ipc::SendError::Refused(reason)) => {
            eprintln!("vinowhisper-tray: {reason}");
            return ExitCode::FAILURE;
        }
        Err(ipc::SendError::Io(err)) => {
            eprintln!("vinowhisper-tray: could not reach the running instance: {err}");
            return ExitCode::FAILURE;
        }
    };
    if !has_wayland_session() {
        eprintln!(
            "vinowhisper-tray: no Wayland session here, so the running tray was left alone; \
             quit it from its menu and start it again to use this version"
        );
        return ExitCode::FAILURE;
    }
    let exe = match std::env::current_exe() {
        Ok(exe) => exe,
        Err(err) => {
            eprintln!("vinowhisper-tray: cannot find this binary: {err}");
            return ExitCode::FAILURE;
        }
    };
    let old = restart::cmdline(pid).unwrap_or_default();
    let relaunch = relaunch_args(old.get(1..).unwrap_or_default());

    if let Err(err) = ipc::send(Request::Quit)
        && !matches!(err, ipc::SendError::NotRunning)
    {
        eprintln!("vinowhisper-tray: could not stop the running instance: {err:?}");
        return ExitCode::FAILURE;
    }
    if !restart::wait_until(restart::EXIT_WAIT, || restart::exited(pid)) {
        eprintln!(
            "vinowhisper-tray: the running instance (pid {}) has not exited after {}s; \
             not starting another",
            pid.as_raw_nonzero(),
            restart::EXIT_WAIT.as_secs()
        );
        return ExitCode::FAILURE;
    }
    if let Err(err) = restart::spawn_detached(&exe, &relaunch) {
        eprintln!("vinowhisper-tray: could not start {}: {err}", exe.display());
        return ExitCode::FAILURE;
    }
    let started = restart::wait_until(restart::START_WAIT, || ipc::send(Request::Ping).is_ok());
    if !started {
        eprintln!(
            "vinowhisper-tray: started {} but it is not answering; run it by hand to see why",
            exe.display()
        );
        return ExitCode::FAILURE;
    }
    println!("restarted vinowhisper-tray, in the tray (captions are off until shown)");
    ExitCode::SUCCESS
}

fn has_wayland_session() -> bool {
    std::env::var_os("WAYLAND_DISPLAY").is_some_and(|display| !display.is_empty())
}

// (pinned by `tests::a_restart_keeps_the_source_and_caption_but_not_the_visibility`)
fn relaunch_args(old: &[String]) -> Vec<String> {
    let mut args = vec!["--hidden".to_owned()];
    let Ok(old) = Args::parse(old.iter().cloned()) else {
        return args;
    };
    if let Some(source) = old.source {
        args.extend(["--source".to_owned(), source.as_arg().to_owned()]);
    }
    if let Some(caption) = old.caption {
        args.extend(["--caption".to_owned(), caption.display().to_string()]);
    }
    args
}

fn run_install(args: &Args) -> ExitCode {
    let caption = args.caption.as_deref();
    let result = if let Some(dir) = &args.export_desktop {
        install::export(dir)
    } else if args.uninstall {
        install::uninstall()
    } else if args.install {
        install::install(args.autostart, caption)
    } else {
        install::autostart(caption)
    };
    match result {
        Ok(paths) => {
            let verb = if args.uninstall { "removed" } else { "wrote" };
            for path in paths {
                println!("{verb} {}", path.display());
            }
            let integrating = args.install || args.autostart;
            if integrating && session::find_caption(args.caption.as_deref()).is_none() {
                eprintln!("note: {}", session::not_found_message());
            }
            ExitCode::SUCCESS
        }
        Err(err) => {
            eprintln!("vinowhisper-tray: {err}");
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
    fn only_show_toggle_and_ping_start_a_tray() {
        for request in [Request::Show, Request::Toggle, Request::Ping] {
            assert!(starts_an_instance(request), "{request:?}");
        }
    }

    #[test]
    fn a_request_for_a_running_tray_does_not_start_one() {
        for request in [
            Request::Shortcuts,
            Request::Hide,
            Request::Quit,
            Request::Reload,
            Request::ConfigureShortcut,
            Request::Dictate,
            Request::DictatePress,
            Request::DictateRelease,
            Request::SummariesReady,
        ] {
            assert!(!starts_an_instance(request), "{request:?}");
        }
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
    fn reload_is_a_command() {
        let args = Args::parse(["reload".to_owned()]).unwrap();
        assert_eq!(args.command, Some(Request::Reload));
    }

    #[test]
    fn restart_is_a_command_of_its_own() {
        let args = parse(&["restart"]).unwrap();
        assert!(args.restart && args.command.is_none());
        assert!(parse(&["restart", "show"]).is_err());
        assert!(parse(&["show", "restart"]).is_err());
    }

    #[test]
    fn a_restart_keeps_the_source_and_caption_but_not_the_visibility() {
        let old =
            |args: &[&str]| relaunch_args(&args.iter().map(|a| a.to_string()).collect::<Vec<_>>());
        assert_eq!(old(&["--hidden"]), ["--hidden"]);
        assert_eq!(old(&["show"]), ["--hidden"]);
        assert_eq!(
            old(&["--source", "mic", "--caption", "/opt/vw/caption"]),
            [
                "--hidden",
                "--source",
                "mic",
                "--caption",
                "/opt/vw/caption"
            ]
        );
        assert_eq!(old(&["--made-up"]), ["--hidden"]);
    }

    #[test]
    fn bad_input_is_refused_with_a_reason() {
        assert!(
            parse(&["--source", "speakers"])
                .unwrap_err()
                .contains("'output' or 'mic'")
        );
        assert!(parse(&["reboot"]).unwrap_err().contains("unknown command"));
        assert_eq!(
            parse(&["dictate-press"]).unwrap().command,
            Some(Request::DictatePress)
        );
        assert!(parse(&["ping"]).is_err(), "ping is internal, not a command");
        assert!(parse(&["show", "hide"]).is_err());
        assert!(parse(&["--frobnicate"]).is_err());
        assert!(parse(&["--export-desktop"]).is_err(), "needs a directory");
        assert!(parse(&["--export-desktop", "/tmp/x", "--install"]).is_err());
        assert!(parse(&["--uninstall", "--autostart"]).is_err());
    }

    #[test]
    fn an_option_and_its_value_are_two_arguments_never_one() {
        assert_eq!(
            parse(&["--source", "mic"]).unwrap().source,
            Some(Source::Mic)
        );
        assert_eq!(
            parse(&["--source=mic"]).unwrap_err(),
            "unknown option --source=mic"
        );
    }

    #[test]
    fn short_flags_do_not_combine_and_a_bare_dash_is_no_command() {
        assert!(parse(&["-h"]).unwrap().help);
        assert!(parse(&["-V"]).unwrap().version);
        assert_eq!(parse(&["-hV"]).unwrap_err(), "unknown option -hV");
        assert_eq!(parse(&["--"]).unwrap_err(), "unknown option --");
        assert_eq!(parse(&["-"]).unwrap_err(), "unknown option -");
    }

    #[test]
    fn a_missing_value_names_what_the_option_needs() {
        assert_eq!(
            parse(&["--source"]).unwrap_err(),
            "--source needs 'output' or 'mic'"
        );
        assert_eq!(parse(&["--caption"]).unwrap_err(), "--caption needs a path");
        assert_eq!(
            parse(&["show", "hide"]).unwrap_err(),
            "one command at a time, got a second: \"hide\""
        );
    }

    #[test]
    fn autostart_stands_alone_for_a_packaged_copy() {
        let args = parse(&["--autostart"]).unwrap();
        assert!(args.autostart && !args.install);
    }

    #[test]
    fn export_desktop_takes_a_directory() {
        let args = parse(&["--export-desktop", "/buildroot/usr/share"]).unwrap();
        assert_eq!(
            args.export_desktop,
            Some(PathBuf::from("/buildroot/usr/share"))
        );
    }
}
