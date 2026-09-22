use std::future::Future;
use std::os::unix::fs::OpenOptionsExt;
use std::path::PathBuf;
use std::thread;
use std::time::Duration;

use ashpd::desktop::remote_desktop::{
    DeviceType, KeyState, RemoteDesktop, SelectDevicesOptions, StartOptions,
};
use ashpd::desktop::{PersistMode, ResponseError};
use futures_lite::future;
use smithay_client_toolkit::reexports::calloop::channel::Sender;

use crate::app::Command;
use crate::settings;
use crate::uinput::{self, Keyboard};

// A stale portal session can hang a call, so every one is bounded.
const CALL: Duration = Duration::from_secs(3);
// Start is where the permission dialog appears the first time: the user has to read it.
const DIALOG: Duration = Duration::from_secs(90);

// evdev codes, not keysyms: Shift and Insert sit at these on every layout.
const KEY_LEFTSHIFT: i32 = uinput::KEY_LEFTSHIFT as i32;
const KEY_INSERT: i32 = uinput::KEY_INSERT as i32;

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
                "typing was not allowed. Paste it yourself, or restart vinowhisper-gui to be asked again".into()
            }
            Failure::Unavailable(why) => format!("no remote-desktop portal here ({why})"),
            Failure::Failed(why) => why.clone(),
        }
    }
}

pub struct Paster {
    requests: async_channel::Sender<()>,
}

impl Paster {
    pub fn spawn(tx: Sender<Command>) -> Paster {
        let (requests, incoming) = async_channel::unbounded();
        let spawned = thread::Builder::new()
            .name("portal-paste".into())
            .spawn(move || future::block_on(run(incoming, tx)));
        if let Err(err) = spawned {
            eprintln!("[vinowhisper-gui] dictated text will not be typed: {err}");
        }
        Paster { requests }
    }

    pub fn paste(&self) -> bool {
        self.requests.try_send(()).is_ok()
    }
}

fn token_path() -> PathBuf {
    settings::state_home().join("vinowhisper/remote-desktop.token")
}

async fn run(requests: async_channel::Receiver<()>, tx: Sender<Command>) {
    // Preferred: KDE announces every portal session with a notification and a tray icon.
    let mut keyboard = match Keyboard::open() {
        Ok(keyboard) => Some(keyboard),
        Err(err) => {
            eprintln!(
                "[vinowhisper-gui] /dev/uinput: {err}; dictation will type through the \
                 remote-desktop portal, which the desktop announces each time"
            );
            None
        }
    };
    let mut stuck: Option<Failure> = None;
    while requests.recv().await.is_ok() {
        if let Some(device) = &mut keyboard {
            match device.shift_insert() {
                Ok(()) => {
                    let _ = tx.send(Command::Pasted(Ok(())));
                    continue;
                }
                Err(err) => {
                    eprintln!(
                        "[vinowhisper-gui] /dev/uinput stopped working ({err}); using the portal"
                    );
                    keyboard = None;
                }
            }
        }
        let result = match &stuck {
            Some(failure) => Err(failure.clone()),
            None => paste_once().await,
        };
        if let Err(failure) = &result {
            eprintln!("[vinowhisper-gui] paste: {}", failure.message());
            if failure.sticks() {
                stuck = Some(failure.clone());
            }
        }
        let _ = tx.send(Command::Pasted(result.map_err(|failure| failure.message())));
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

async fn paste_once() -> Result<(), Failure> {
    let portal = bounded(CALL, "connecting", RemoteDesktop::new()).await?;
    let session = bounded(
        CALL,
        "creating a session",
        portal.create_session(Default::default()),
    )
    .await?;
    let result = keys(&portal, &session).await;
    let _ = bounded(CALL, "closing the session", session.close()).await;
    result
}

async fn keys(
    portal: &RemoteDesktop,
    session: &ashpd::desktop::Session<RemoteDesktop>,
) -> Result<(), Failure> {
    let path = token_path();
    let token = std::fs::read_to_string(&path).ok();
    let options = SelectDevicesOptions::default()
        .set_devices(Some(DeviceType::Keyboard.into()))
        .set_persist_mode(PersistMode::ExplicitlyRevoked)
        .set_restore_token(token.as_deref().map(str::trim));
    bounded(CALL, "selecting the keyboard", async {
        portal.select_devices(session, options).await?.response()
    })
    .await?;
    let started = bounded(DIALOG, "starting", async {
        portal
            .start(session, None, StartOptions::default())
            .await?
            .response()
    })
    .await?;
    // A token is good for one start; the next one needs the replacement.
    if let Some(next) = started.restore_token() {
        save_token(&path, next);
    }
    if !started.devices().contains(DeviceType::Keyboard) {
        return Err(Failure::Denied);
    }
    for (code, state) in [
        (KEY_LEFTSHIFT, KeyState::Pressed),
        (KEY_INSERT, KeyState::Pressed),
        (KEY_INSERT, KeyState::Released),
        (KEY_LEFTSHIFT, KeyState::Released),
    ] {
        bounded(
            CALL,
            "pressing Shift+Insert",
            portal.notify_keyboard_keycode(session, code, state, Default::default()),
        )
        .await?;
    }
    Ok(())
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
            "[vinowhisper-gui] could not save {}, so the desktop will ask again next time: {err}",
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
}
