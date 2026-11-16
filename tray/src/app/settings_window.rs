use super::*;

pub(super) fn spawn_settings(args: &[String]) -> std::io::Result<()> {
    // Next to this binary first, as setup installs them, then PATH.
    let beside = std::env::current_exe()
        .ok()
        .and_then(|exe| exe.parent().map(|dir| dir.join("vinowhisper-gui")))
        .filter(|path| path.is_file());
    let command = beside.unwrap_or_else(|| PathBuf::from("vinowhisper-gui"));
    match std::process::Command::new(&command)
        .args(args)
        .stdin(std::process::Stdio::null())
        .spawn()
    {
        Ok(mut child) => {
            // Reaped off the event loop; the window outlives nothing it needs from us.
            std::thread::spawn(move || {
                let _ = child.wait();
            });
            Ok(())
        }
        Err(err) => {
            eprintln!(
                "[vinowhisper-tray] could not open {}: {err}",
                command.display()
            );
            Err(err)
        }
    }
}

pub(super) fn open_settings_window(args: &[String]) {
    let _ = spawn_settings(args);
}
