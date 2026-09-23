use std::io;
use std::os::fd::{AsFd, OwnedFd};
use std::time::Duration;

use rustix::time::{
    Itimerspec, TimerfdClockId, TimerfdFlags, TimerfdTimerFlags, Timespec, timerfd_create,
    timerfd_settime,
};

// CLOCK_BOOTTIME, not the monotonic clock calloop's Timer uses: time asleep counts as idle.
pub struct IdleTimer(OwnedFd);

impl IdleTimer {
    pub fn new() -> io::Result<Self> {
        let fd = timerfd_create(
            TimerfdClockId::Boottime,
            TimerfdFlags::CLOEXEC | TimerfdFlags::NONBLOCK,
        )?;
        Ok(Self(fd))
    }

    pub fn watcher(&self) -> io::Result<OwnedFd> {
        self.0.try_clone()
    }

    pub fn arm(&self, after: Duration) -> io::Result<()> {
        self.set(after.max(Duration::from_nanos(1)))
    }

    pub fn disarm(&self) -> io::Result<()> {
        self.set(Duration::ZERO)
    }

    fn set(&self, after: Duration) -> io::Result<()> {
        let zero = Timespec {
            tv_sec: 0,
            tv_nsec: 0,
        };
        let value = Timespec {
            tv_sec: after.as_secs().try_into().unwrap_or(i64::MAX),
            tv_nsec: after.subsec_nanos().into(),
        };
        timerfd_settime(
            &self.0,
            TimerfdTimerFlags::empty(),
            &Itimerspec {
                it_interval: zero,
                it_value: value,
            },
        )?;
        Ok(())
    }
}

pub fn fired(fd: impl AsFd) -> bool {
    let mut expirations = [0u8; 8];
    rustix::io::read(fd, &mut expirations).is_ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn an_armed_timer_fires_once_and_is_then_quiet() {
        let timer = IdleTimer::new().unwrap();
        let watcher = timer.watcher().unwrap();
        assert!(!fired(&watcher));
        timer.arm(Duration::from_millis(10)).unwrap();
        std::thread::sleep(Duration::from_millis(30));
        assert!(fired(&watcher));
        assert!(!fired(&watcher));
    }

    #[test]
    fn disarming_drops_an_expiry_nobody_read_yet() {
        let timer = IdleTimer::new().unwrap();
        timer.arm(Duration::from_millis(10)).unwrap();
        std::thread::sleep(Duration::from_millis(30));
        timer.disarm().unwrap();
        assert!(!fired(timer.watcher().unwrap()));
    }

    #[test]
    fn rearming_starts_the_wait_over() {
        let timer = IdleTimer::new().unwrap();
        timer.arm(Duration::from_millis(10)).unwrap();
        timer.arm(Duration::from_secs(60)).unwrap();
        std::thread::sleep(Duration::from_millis(30));
        assert!(!fired(timer.watcher().unwrap()));
    }
}
