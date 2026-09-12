//! The `vinowhisper-caption --json` child: found, started, read and stopped.

use std::collections::VecDeque;
use std::io::{self, BufRead, BufReader};
use std::os::unix::fs::PermissionsExt;
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Command as Process, Stdio};
use std::sync::{Arc, Mutex};
use std::thread;

use rustix::process::{Pid, Signal, kill_process, set_parent_process_death_signal};
use smithay_client_toolkit::reexports::calloop::channel::Sender;

use crate::app::Command;
use crate::protocol;
use crate::settings::{self, Source};

const PROGRAM: &str = "vinowhisper-caption";

/// Lines of the child's stderr kept for explaining an exit it did not
/// explain itself.
const STDERR_TAIL: usize = 8;

/// Where the caption command is, in order: `--caption`, `$VINOWHISPER_CAPTION`,
/// `$PATH`, then the places `$PATH` tends to miss.
pub fn find_caption(explicit: Option<&Path>) -> Option<PathBuf> {
    if let Some(path) = explicit {
        return Some(path.to_owned());
    }
    if let Some(path) = std::env::var_os("VINOWHISPER_CAPTION").filter(|value| !value.is_empty()) {
        return Some(PathBuf::from(path));
    }
    let on_path = std::env::var_os("PATH")
        .map(|path| std::env::split_paths(&path).collect::<Vec<_>>())
        .unwrap_or_default();
    // A desktop launcher or a login autostart often runs with a PATH that
    // lacks ~/.local/bin, which is exactly where vinowhisper-setup links the
    // commands. Next to this binary covers a venv's bin/ as well.
    let fallbacks = [
        Some(settings::home().join(".local/bin")),
        std::env::current_exe()
            .ok()
            .and_then(|exe| exe.parent().map(Path::to_path_buf)),
    ];
    on_path
        .into_iter()
        .chain(fallbacks.into_iter().flatten())
        .map(|dir| dir.join(PROGRAM))
        .find(|candidate| is_executable(candidate))
}

fn is_executable(path: &Path) -> bool {
    std::fs::metadata(path)
        .map(|meta| meta.is_file() && meta.permissions().mode() & 0o111 != 0)
        .unwrap_or(false)
}

pub fn not_found_message() -> String {
    format!(
        "{PROGRAM} was not found. Install vinoWhisper (pip install vinowhisper, then \
         vinowhisper-setup), or point at it with vinowhisper-gui --caption PATH."
    )
}

pub struct Session {
    pub generation: u64,
    pid: Pid,
    stopping: bool,
}

impl Session {
    /// Start a caption process. Its events arrive as `Command::Caption`, and
    /// its exit as `Command::CaptionExited`, both tagged with `generation` so
    /// that a session being replaced cannot talk over the one replacing it.
    ///
    /// Must be called from the main thread: Linux delivers the parent-death
    /// signal when the *thread* that forked exits, not the process, so a
    /// child spawned from a short-lived thread would be killed with it.
    pub fn start(
        program: &Path,
        source: Source,
        generation: u64,
        tx: Sender<Command>,
    ) -> io::Result<Session> {
        let mut process = Process::new(program);
        process
            .args(["--json", "--source", source.as_arg()])
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        // If this process dies without stopping the child (a crash, a
        // SIGKILL), the child is told to go too, rather than capturing audio
        // and keeping the NPU busy for nobody.
        //
        // SAFETY: runs between fork and exec, so it must be
        // async-signal-safe. prctl is, and nothing here allocates.
        unsafe {
            process.pre_exec(|| {
                set_parent_process_death_signal(Some(Signal::TERM)).map_err(io::Error::from)
            });
        }
        let mut child = process.spawn()?;
        let pid = Pid::from_child(&child);
        let stdout = child.stdout.take().expect("stdout is piped");
        let stderr = child.stderr.take().expect("stderr is piped");

        let tail = Arc::new(Mutex::new(VecDeque::with_capacity(STDERR_TAIL)));
        let stderr_reader = {
            let tail = Arc::clone(&tail);
            thread::Builder::new()
                .name("caption-stderr".into())
                .spawn(move || {
                    for line in BufReader::new(stderr).lines().map_while(Result::ok) {
                        // Passed through as well as kept: this is where the
                        // Python side's human-readable diagnostics go, and a
                        // terminal or the journal is where someone reads them.
                        eprintln!("[caption] {line}");
                        let mut tail = tail.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
                        if tail.len() == STDERR_TAIL {
                            tail.pop_front();
                        }
                        tail.push_back(line);
                    }
                })?
        };

        thread::Builder::new()
            .name("caption-stdout".into())
            .spawn(move || {
                for line in BufReader::new(stdout).lines().map_while(Result::ok) {
                    let Some(event) = protocol::parse(&line) else {
                        continue;
                    };
                    if tx.send(Command::Caption { generation, event }).is_err() {
                        break;
                    }
                }
                let status = child.wait();
                let _ = stderr_reader.join();
                let stderr = tail
                    .lock()
                    .map(|tail| {
                        tail.iter()
                            .filter(|line| !line.trim().is_empty())
                            .cloned()
                            .collect()
                    })
                    .unwrap_or_default();
                let _ = tx.send(Command::CaptionExited {
                    generation,
                    success: status.as_ref().is_ok_and(|status| status.success()),
                    status: status.map_or_else(|err| err.to_string(), |status| status.to_string()),
                    stderr,
                });
            })?;

        Ok(Session {
            generation,
            pid,
            stopping: false,
        })
    }

    /// Ctrl+C, as far as the Python side can tell. It releases the words
    /// still waiting on a second cycle, emits Stopped, and stops pw-record on
    /// its way out.
    pub fn interrupt(&mut self) {
        self.stopping = true;
        let _ = kill_process(self.pid, Signal::INT);
    }

    /// For a process that ignored `interrupt`.
    pub fn kill(&self) {
        let _ = kill_process(self.pid, Signal::KILL);
    }

    pub fn stopping(&self) -> bool {
        self.stopping
    }
}
