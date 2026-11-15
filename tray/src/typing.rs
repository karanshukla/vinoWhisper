use std::future::Future;
use std::os::unix::fs::OpenOptionsExt;
use std::path::PathBuf;
use std::thread;
use std::time::Duration;

use ashpd::desktop::remote_desktop::{
    DeviceType, KeyState, RemoteDesktop, SelectDevicesOptions, StartOptions,
};
use ashpd::desktop::{PersistMode, ResponseError, Session};
use futures_lite::future;
use smithay_client_toolkit::reexports::calloop::channel::Sender;

use crate::app::Command;
use crate::settings;

// A stale portal session can hang a call, so every one is bounded.
const CALL: Duration = Duration::from_secs(3);
// Start is where the permission dialog appears the first time: the user has to read it.
const DIALOG: Duration = Duration::from_secs(90);

#[derive(Debug, Clone, PartialEq)]
enum Failure {
    Denied,
    Unavailable(String),
    Failed(String),
}

impl Failure {
    fn sticks(&self) -> bool {
        !matches!(self, Failure::Failed(_))
    }

    fn message(&self) -> String {
        match self {
            Failure::Denied => {
                "typing was not allowed; restart vinowhisper-tray to be asked again".into()
            }
            Failure::Unavailable(why) => {
                format!("this desktop has no remote-desktop portal ({why})")
            }
            Failure::Failed(why) => why.clone(),
        }
    }
}

enum Request {
    Type(String),
    IdleMinutes(u64),
}

pub struct Typist {
    requests: async_channel::Sender<Request>,
}

impl Typist {
    pub fn spawn(tx: Sender<Command>, idle_minutes: u64) -> Typist {
        let (requests, incoming) = async_channel::unbounded();
        let spawned = thread::Builder::new()
            .name("portal-typing".into())
            .spawn(move || future::block_on(run(incoming, tx, idle_minutes)));
        if let Err(err) = spawned {
            eprintln!("[vinowhisper-tray] dictated text will not be typed: {err}");
        }
        Typist { requests }
    }

    pub fn type_text(&self, text: String) -> bool {
        self.requests.try_send(Request::Type(text)).is_ok()
    }

    pub fn set_idle_minutes(&self, minutes: u64) {
        let _ = self.requests.try_send(Request::IdleMinutes(minutes));
    }
}

fn token_path() -> PathBuf {
    settings::state_home().join("vinowhisper/remote-desktop.token")
}

struct Open {
    portal: RemoteDesktop,
    session: Session<RemoteDesktop>,
}

impl Open {
    async fn close(self) {
        let _ = bounded(CALL, "closing the session", self.session.close()).await;
    }
}

// KDE announces each new session, so one is kept between dictations and closed once idle.
async fn run(
    requests: async_channel::Receiver<Request>,
    tx: Sender<Command>,
    mut idle_minutes: u64,
) {
    let mut open: Option<Open> = None;
    let mut stuck: Option<Failure> = None;
    loop {
        let request = match (&open, idle(idle_minutes)) {
            (Some(_), Some(limit)) => {
                future::or(async { requests.recv().await.ok() }, async {
                    async_io::Timer::after(limit).await;
                    None
                })
                .await
            }
            _ => match requests.recv().await {
                Ok(request) => Some(request),
                Err(_) => break,
            },
        };
        let text = match request {
            None => {
                if requests.is_closed() {
                    break;
                }
                if let Some(session) = open.take() {
                    session.close().await;
                }
                continue;
            }
            Some(Request::IdleMinutes(minutes)) => {
                idle_minutes = minutes;
                continue;
            }
            Some(Request::Type(text)) => text,
        };
        let result = match &stuck {
            Some(failure) => Err(failure.clone()),
            None => type_text(&mut open, &text).await,
        };
        if let Err(failure) = &result {
            eprintln!("[vinowhisper-tray] typing: {}", failure.message());
            if failure.sticks() {
                stuck = Some(failure.clone());
            }
        }
        let _ = tx.send(Command::Typed(result.map_err(|failure| failure.message())));
    }
    if let Some(session) = open.take() {
        session.close().await;
    }
}

fn idle(minutes: u64) -> Option<Duration> {
    (minutes > 0).then(|| Duration::from_secs(minutes * 60))
}

// A kept session can have been closed by the desktop meanwhile: one new one is tried, and
// only before the first key, so nothing is typed twice.
async fn type_text(open: &mut Option<Open>, text: &str) -> Result<(), Failure> {
    let keysyms: Vec<i32> = text.chars().filter_map(keysym).collect();
    let reused = open.is_some();
    if open.is_none() {
        *open = Some(start().await?);
    }
    let session = open.as_ref().expect("opened above");
    let first = match keysyms.first() {
        Some(&first) => press(session, first).await,
        None => return Ok(()),
    };
    if let Err(failure) = first {
        if let Some(stale) = open.take() {
            stale.close().await;
        }
        if !reused {
            return Err(failure);
        }
        *open = Some(start().await?);
        press(open.as_ref().expect("opened above"), keysyms[0]).await?;
    }
    let session = open.as_ref().expect("opened above");
    for &sym in &keysyms[1..] {
        if let Err(failure) = press(session, sym).await {
            if let Some(broken) = open.take() {
                broken.close().await;
            }
            return Err(failure);
        }
    }
    Ok(())
}

async fn press(open: &Open, sym: i32) -> Result<(), Failure> {
    for state in [KeyState::Pressed, KeyState::Released] {
        bounded(
            CALL,
            "typing",
            open.portal
                .notify_keyboard_keysym(&open.session, sym, state, Default::default()),
        )
        .await?;
    }
    Ok(())
}

async fn start() -> Result<Open, Failure> {
    let portal = bounded(CALL, "connecting", RemoteDesktop::new()).await?;
    let session = bounded(
        CALL,
        "creating a session",
        portal.create_session(Default::default()),
    )
    .await?;
    let path = token_path();
    let token = std::fs::read_to_string(&path).ok();
    let options = SelectDevicesOptions::default()
        .set_devices(Some(DeviceType::Keyboard.into()))
        .set_persist_mode(PersistMode::ExplicitlyRevoked)
        .set_restore_token(token.as_deref().map(str::trim));
    let started = async {
        bounded(CALL, "selecting the keyboard", async {
            portal.select_devices(&session, options).await?.response()
        })
        .await?;
        bounded(DIALOG, "starting", async {
            portal
                .start(&session, None, StartOptions::default())
                .await?
                .response()
        })
        .await
    }
    .await;
    let started = match started {
        Ok(started) => started,
        Err(failure) => {
            let _ = bounded(CALL, "closing the session", session.close()).await;
            return Err(failure);
        }
    };
    // A token is good for one start; the next one needs the replacement.
    if let Some(next) = started.restore_token() {
        save_token(&path, next);
    }
    if !started.devices().contains(DeviceType::Keyboard) {
        let _ = bounded(CALL, "closing the session", session.close()).await;
        return Err(Failure::Denied);
    }
    Ok(Open { portal, session })
}

// Keysyms, not keycodes, so the desktop's own layout decides which key makes each character.
fn keysym(c: char) -> Option<i32> {
    let code = c as u32;
    match c {
        // Never Return or Tab: a pasted line break was safe in a terminal, a typed one runs it.
        '\n' | '\t' => Some(0x20),
        _ if c.is_control() => None,
        // Latin-1 keysyms are the code points; everything else is 0x01000000 + the code point.
        _ if (0x20..0x7f).contains(&code) || (0xa0..0x100).contains(&code) => Some(code as i32),
        _ => Some((0x0100_0000 + code) as i32),
    }
}

async fn bounded<T>(
    limit: Duration,
    what: &str,
    call: impl Future<Output = Result<T, ashpd::Error>>,
) -> Result<T, Failure> {
    future::or(
        async {
            call.await.map_err(|err| match err {
                ashpd::Error::Response(ResponseError::Cancelled) => Failure::Denied,
                ashpd::Error::PortalNotFound(_) => Failure::Unavailable(err.to_string()),
                err => Failure::Failed(format!("{what}: {err}")),
            })
        },
        async {
            async_io::Timer::after(limit).await;
            Err(Failure::Failed(format!(
                "{what}: no answer from the portal in {}s",
                limit.as_secs()
            )))
        },
    )
    .await
}

fn save_token(path: &PathBuf, token: &str) {
    let written = path
        .parent()
        .map_or(Ok(()), std::fs::create_dir_all)
        .and_then(|()| {
            use std::io::Write;
            std::fs::OpenOptions::new()
                .write(true)
                .create(true)
                .truncate(true)
                .mode(0o600)
                .open(path)
                .and_then(|mut file| file.write_all(token.as_bytes()))
        });
    if let Err(err) = written {
        eprintln!(
            "[vinowhisper-tray] could not save {}, so the desktop will ask again next time: {err}",
            path.display()
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_refusal_or_a_missing_portal_is_remembered_and_a_glitch_is_not() {
        assert!(Failure::Denied.sticks());
        assert!(Failure::Unavailable("none".into()).sticks());
        assert!(!Failure::Failed("timeout".into()).sticks());
    }

    #[test]
    fn a_refusal_says_how_to_be_asked_again() {
        assert!(Failure::Denied.message().contains("restart"));
    }

    #[test]
    fn the_token_is_private_to_the_user() {
        let dir = std::env::temp_dir().join(format!("vw-token-{}", std::process::id()));
        let path = dir.join("nested/remote-desktop.token");
        save_token(&path, "abc");
        let mode = std::fs::metadata(&path).unwrap().permissions();
        use std::os::unix::fs::PermissionsExt;
        assert_eq!(mode.mode() & 0o777, 0o600);
        assert_eq!(std::fs::read_to_string(&path).unwrap(), "abc");
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn latin_1_is_its_own_keysym_and_the_rest_is_offset() {
        assert_eq!(keysym('a'), Some(0x61));
        assert_eq!(keysym(' '), Some(0x20));
        assert_eq!(keysym('é'), Some(0xe9));
        assert_eq!(keysym('ß'), Some(0xdf));
        assert_eq!(keysym('—'), Some(0x0100_2014));
        assert_eq!(keysym('€'), Some(0x0100_20ac));
        assert_eq!(keysym('“'), Some(0x0100_201c));
    }

    #[test]
    fn line_breaks_and_tabs_are_spaces_and_other_controls_are_dropped() {
        assert_eq!(keysym('\n'), Some(0x20));
        assert_eq!(keysym('\t'), Some(0x20));
        assert_eq!(keysym('\r'), None);
        assert_eq!(keysym('\u{7f}'), None);
        assert_eq!(keysym('\u{85}'), None);
    }

    #[test]
    fn zero_minutes_keeps_the_session_open() {
        assert_eq!(idle(0), None);
        assert_eq!(idle(5), Some(Duration::from_secs(300)));
    }
}
