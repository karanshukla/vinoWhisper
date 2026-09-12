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

const STDERR_TAIL: usize = 8;

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
    /// Main thread only: the parent-death signal fires when the spawning thread exits.
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
        // SAFETY: between fork and exec; prctl is async-signal-safe and nothing allocates.
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

    pub fn interrupt(&mut self) {
        self.stopping = true;
        let _ = kill_process(self.pid, Signal::INT);
    }

    pub fn kill(&self) {
        let _ = kill_process(self.pid, Signal::KILL);
    }

    pub fn stopping(&self) -> bool {
        self.stopping
    }
}
