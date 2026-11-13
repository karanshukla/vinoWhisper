use std::fs;
use std::io;
use std::os::unix::process::CommandExt;
use std::path::Path;
use std::process::{Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};

use rustix::process::Pid;

const POLL: Duration = Duration::from_millis(100);
pub const EXIT_WAIT: Duration = Duration::from_secs(10);
pub const START_WAIT: Duration = Duration::from_secs(10);

pub fn cmdline(pid: Pid) -> Option<Vec<String>> {
    let raw = fs::read(format!("/proc/{}/cmdline", pid.as_raw_nonzero())).ok()?;
    Some(
        raw.split(|byte| *byte == 0)
            .filter(|arg| !arg.is_empty())
            .map(|arg| String::from_utf8_lossy(arg).into_owned())
            .collect(),
    )
}

pub fn wait_until(timeout: Duration, mut done: impl FnMut() -> bool) -> bool {
    let deadline = Instant::now() + timeout;
    loop {
        if done() {
            return true;
        }
        if Instant::now() >= deadline {
            return false;
        }
        thread::sleep(POLL);
    }
}

pub fn exited(pid: Pid) -> bool {
    let Ok(stat) = fs::read_to_string(format!("/proc/{}/stat", pid.as_raw_nonzero())) else {
        return true;
    };
    is_zombie(&stat)
}

// The comm field may hold spaces and parentheses; the state follows its last ')'.
fn is_zombie(stat: &str) -> bool {
    stat.rsplit_once(')')
        .is_some_and(|(_, rest)| rest.trim_start().starts_with('Z'))
}

// Own session and no inherited pipes: an installer waiting on its output must not wait on the tray.
pub fn spawn_detached(exe: &Path, args: &[String]) -> io::Result<()> {
    let mut command = Command::new(exe);
    command
        .args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    // SAFETY: between fork and exec; setsid is async-signal-safe and nothing allocates.
    unsafe {
        command.pre_exec(|| rustix::process::setsid().map(drop).map_err(io::Error::from));
    }
    command.spawn().map(drop)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_zombie_counts_as_exited() {
        assert!(is_zombie("4242 (vinowhisper-tr) Z 1 4242"));
        assert!(!is_zombie("4242 (vinowhisper-tr) S 1 4242"));
        assert!(!is_zombie("4242 (a) Z) S 1 4242"));
    }

    #[test]
    fn this_process_is_running_and_reads_its_own_cmdline() {
        let me = rustix::process::getpid();
        assert!(!exited(me));
        assert!(!cmdline(me).unwrap().is_empty());
    }

    #[test]
    fn waiting_stops_at_the_deadline() {
        assert!(!wait_until(Duration::from_millis(150), || false));
        assert!(wait_until(Duration::from_secs(5), || true));
    }
}
