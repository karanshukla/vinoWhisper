use std::io::{self, Write};
use std::path::Path;
use std::process::{ChildStdin, Command as Process, Stdio};

use rustix::process::{Pid, Signal, kill_process};
use smithay_client_toolkit::reexports::calloop::channel::Sender;

use crate::app::Command;
use crate::protocol;
use crate::session;

/// A resident vinowhisper-dictate: started ahead of the key so Python's imports are not in the first word.
pub struct Dictator {
    pub generation: u64,
    pid: Pid,
    stdin: ChildStdin,
}

impl Dictator {
    /// Main thread only, see `session::spawn`.
    pub fn start(program: &Path, generation: u64, tx: Sender<Command>) -> io::Result<Dictator> {
        let mut process = Process::new(program);
        process.arg("--json").stdin(Stdio::piped());
        let events = tx.clone();
        let child = session::spawn(
            process,
            "dictate",
            move |line| match protocol::parse(line) {
                Some(event) => events
                    .send(Command::Dictation { generation, event })
                    .is_ok(),
                None => true,
            },
            move |exit| {
                let why = exit
                    .stderr
                    .last()
                    .cloned()
                    .unwrap_or_else(|| format!("vinowhisper-dictate exited ({})", exit.status));
                let _ = tx.send(Command::DictatorExited {
                    generation,
                    message: why,
                });
            },
        )?;
        let stdin = child
            .stdin
            .ok_or_else(|| io::Error::other("stdin is not piped"))?;
        Ok(Dictator {
            generation,
            pid: child.pid,
            stdin,
        })
    }

    pub fn send(&mut self, command: &str) -> io::Result<()> {
        writeln!(self.stdin, "{command}")?;
        self.stdin.flush()
    }
}

impl Drop for Dictator {
    fn drop(&mut self) {
        let _ = kill_process(self.pid, Signal::TERM);
    }
}
