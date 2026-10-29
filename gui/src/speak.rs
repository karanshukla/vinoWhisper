use std::io::{BufRead, BufReader};
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use rustix::process::{Pid, Signal, kill_process_group, set_parent_process_death_signal};

use crate::sessions::Part;

pub fn find_on_path(name: &str) -> Option<PathBuf> {
    use std::os::unix::fs::PermissionsExt;
    std::env::split_paths(&std::env::var_os("PATH")?)
        .map(|dir| dir.join(name))
        .find(|path| {
            path.metadata()
                .is_ok_and(|meta| meta.is_file() && meta.permissions().mode() & 0o111 != 0)
        })
}

// vinoSpeak reading a session aloud: `vinospeak --file BUNDLE --part summary|transcript`
// (karanshukla/vinoSpeak#42). Dropping it stops it.
pub struct Reading {
    pub path: PathBuf,
    // None once it has exited and been reaped.
    child: Option<Child>,
    last_line: Arc<Mutex<String>>,
    stopped: bool,
}

impl Reading {
    pub fn start(program: &Path, path: &Path, part: Part) -> Result<Reading, String> {
        let mut command = Command::new(program);
        command
            .arg("--file")
            .arg(path)
            .args(["--part", part.as_str()])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::piped())
            // Its own group, so Stop also ends whatever it plays through.
            .process_group(0);
        // SAFETY: prctl only, which is async-signal-safe. Like the tray's caption child, it
        // must not outlive the window, even one that crashed; started from the UI thread.
        unsafe {
            command.pre_exec(|| {
                set_parent_process_death_signal(Some(Signal::TERM)).map_err(std::io::Error::from)
            });
        }
        let mut child = command
            .spawn()
            .map_err(|err| format!("could not run vinoSpeak: {err}"))?;
        let last_line = Arc::new(Mutex::new(String::new()));
        if let Some(stderr) = child.stderr.take() {
            let last = Arc::clone(&last_line);
            std::thread::spawn(move || {
                for line in BufReader::new(stderr).lines().map_while(Result::ok) {
                    if !line.trim().is_empty() {
                        *last.lock().unwrap() = line.trim().to_owned();
                    }
                }
            });
        }
        Ok(Reading {
            path: path.to_path_buf(),
            child: Some(child),
            last_line,
            stopped: false,
        })
    }

    // None while it reads; once it has exited, why it failed, if it did.
    pub fn poll(&mut self) -> Option<Result<(), String>> {
        let status = match self.child.as_mut()?.try_wait() {
            Ok(None) => return None,
            Ok(Some(status)) => status,
            Err(err) => {
                self.child = None;
                return Some(Err(err.to_string()));
            }
        };
        self.child = None;
        if status.success() || self.stopped {
            return Some(Ok(()));
        }
        // The pipe can still hold the last line for a moment after the exit.
        std::thread::sleep(Duration::from_millis(50));
        let line = self.last_line.lock().unwrap().clone();
        Some(Err(reason(&line, status.code())))
    }

    pub fn stop(&mut self) {
        if let Some(child) = self.child.as_ref().filter(|_| !self.stopped) {
            self.stopped = true;
            let _ = kill_process_group(Pid::from_child(child), Signal::TERM);
        }
    }
}

impl Drop for Reading {
    fn drop(&mut self) {
        self.stop();
        let Some(mut child) = self.child.take() else {
            return;
        };
        // Reaped off the UI thread; a reader that ignores SIGTERM gets SIGKILL after 2 s.
        std::thread::spawn(move || {
            for _ in 0..20 {
                if !matches!(child.try_wait(), Ok(None)) {
                    return;
                }
                std::thread::sleep(Duration::from_millis(100));
            }
            let _ = kill_process_group(Pid::from_child(&child), Signal::KILL);
            let _ = child.wait();
        });
    }
}

pub fn reason(line: &str, code: Option<i32>) -> String {
    // An older vinoSpeak's argparse refuses the arguments by name.
    if line.contains("unrecognized arguments") && line.contains("--part") {
        return "this vinoSpeak can't read sessions yet. Update vinoSpeak to read them.".to_owned();
    }
    if line.is_empty() {
        return match code {
            Some(code) => format!("vinoSpeak stopped with exit status {code}."),
            None => "vinoSpeak was stopped by a signal.".to_owned(),
        };
    }
    line.to_owned()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn an_old_vinospeak_is_asked_to_update_and_anything_else_says_its_own_words() {
        assert_eq!(
            reason(
                "vinospeak: error: unrecognized arguments: --part summary",
                Some(2)
            ),
            "this vinoSpeak can't read sessions yet. Update vinoSpeak to read them."
        );
        assert_eq!(
            reason("no voice named af_heart", Some(1)),
            "no voice named af_heart"
        );
        assert_eq!(reason("", Some(3)), "vinoSpeak stopped with exit status 3.");
    }
}
