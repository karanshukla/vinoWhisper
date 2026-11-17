use std::fs::File;
use std::future::Future;
use std::io::Write;
use std::os::unix::fs::OpenOptionsExt;
use std::path::PathBuf;
use std::pin::Pin;
use std::thread;
use std::time::{Duration, Instant};

use ashpd::desktop::clipboard::{Clipboard, RequestClipboardOptions, SetSelectionOptions};
use ashpd::desktop::remote_desktop::{
    DeviceType, KeyState, RemoteDesktop, SelectDevicesOptions, StartOptions,
};
use ashpd::desktop::{PersistMode, ResponseError, Session};
use futures_lite::{Stream, StreamExt, future};
use smithay_client_toolkit::reexports::calloop::channel::Sender;

use crate::app::{Command, trace};
use crate::settings;

const CALL_TIMEOUT: Duration = Duration::from_secs(3);
// Long enough for the user to read the first start's permission dialog.
const PERMISSION_DIALOG_TIMEOUT: Duration = Duration::from_secs(90);

const MIME_TYPES: [&str; 5] = [
    "text/plain;charset=utf-8",
    "text/plain",
    "UTF8_STRING",
    "STRING",
    "TEXT",
];
// Klipper, and clipboard managers that copy it, keep nothing that carries this.
const PASSWORD_HINT: &str = "x-kde-passwordManagerHint";

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
                "the desktop did not allow it; restart vinowhisper-tray to be asked again".into()
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
    Copy(String),
    IdleMinutes(u64),
}

pub struct Portal {
    requests: async_channel::Sender<Request>,
}

impl Portal {
    pub fn spawn(tx: Sender<Command>, idle_minutes: u64) -> Portal {
        let (requests, incoming) = async_channel::unbounded();
        let spawned = thread::Builder::new()
            .name("portal".into())
            .spawn(move || future::block_on(run(incoming, tx, idle_minutes)));
        if let Err(err) = spawned {
            eprintln!("[vinowhisper-tray] dictated text will not be typed: {err}");
        }
        Portal { requests }
    }

    pub fn type_text(&self, text: String) -> bool {
        self.requests.try_send(Request::Type(text)).is_ok()
    }

    pub fn copy(&self, text: String) -> bool {
        self.requests.try_send(Request::Copy(text)).is_ok()
    }

    pub fn set_idle_minutes(&self, minutes: u64) {
        let _ = self.requests.try_send(Request::IdleMinutes(minutes));
    }
}

fn token_path() -> PathBuf {
    settings::state_home().join("vinowhisper/remote-desktop.token")
}

type Transfers = Pin<Box<dyn Stream<Item = (String, u32)> + Send>>;

struct Open {
    portal: RemoteDesktop,
    session: Session<RemoteDesktop>,
    clipboard: Option<PortalClipboard>,
    // Kept until the next copy or the session closes: the desktop reads the offer at once
    // (the secret hint, then the text), so a read is not a paste.
    offer: Option<String>,
}

struct PortalClipboard {
    proxy: Clipboard,
    transfers: Transfers,
}

impl Open {
    async fn close(self) {
        let _ = bounded(CALL_TIMEOUT, "closing the session", self.session.close()).await;
    }
}

enum Event {
    Request(Option<Request>),
    Idle,
    Transfer(String, u32),
    TransfersEnded,
}

impl Event {
    fn name(&self) -> String {
        match self {
            Event::Request(None) => "requests closed".into(),
            Event::Request(Some(Request::Type(text))) => {
                format!("type ({} chars)", text.chars().count())
            }
            Event::Request(Some(Request::Copy(text))) => {
                format!("copy ({} chars)", text.chars().count())
            }
            Event::Request(Some(Request::IdleMinutes(minutes))) => format!("idle {minutes} min"),
            Event::Idle => "idle, closing the session".into(),
            Event::Transfer(mime, serial) => format!("paste of {mime} (serial {serial})"),
            Event::TransfersEnded => "paste requests ended, closing the session".into(),
        }
    }
}

struct State {
    open: Option<Open>,
    stuck: Option<Failure>,
    idle_minutes: u64,
    last_used: Instant,
}

// KDE announces each new session, so one is kept between dictations and closed once idle.
// It also holds the clipboard: a copied text lasts until the session closes.
async fn run(requests: async_channel::Receiver<Request>, tx: Sender<Command>, idle_minutes: u64) {
    let mut state = State {
        open: None,
        stuck: None,
        idle_minutes,
        last_used: Instant::now(),
    };
    loop {
        let idle_at = state
            .open
            .as_ref()
            .and(idle(state.idle_minutes))
            .map(|limit| state.last_used + limit);
        let event = next_event(&requests, &mut state.open, idle_at).await;
        trace(format_args!("portal: {}", event.name()));
        match event {
            Event::Request(None) => break,
            Event::Request(Some(Request::IdleMinutes(minutes))) => state.idle_minutes = minutes,
            Event::Request(Some(Request::Type(text))) => {
                state.last_used = Instant::now();
                let result = state.type_or_copy(&text).await;
                let _ = tx.send(Command::Typed(result));
            }
            Event::Request(Some(Request::Copy(text))) => {
                state.last_used = Instant::now();
                let result = state.copy(text).await.map_err(|failure| failure.message());
                let _ = tx.send(Command::Copied(result));
            }
            Event::Idle | Event::TransfersEnded => {
                if let Some(open) = state.open.take() {
                    open.close().await;
                }
            }
            Event::Transfer(mime, serial) => {
                if let Some(open) = &mut state.open {
                    serve(open, &mime, serial).await;
                }
            }
        }
    }
    if let Some(open) = state.open.take() {
        open.close().await;
    }
}

async fn next_event(
    requests: &async_channel::Receiver<Request>,
    open: &mut Option<Open>,
    idle_at: Option<Instant>,
) -> Event {
    let request = async { Event::Request(requests.recv().await.ok()) };
    let idle = async {
        match idle_at {
            Some(at) => {
                async_io::Timer::at(at).await;
                Event::Idle
            }
            None => future::pending().await,
        }
    };
    let transfer = async {
        match open.as_mut().and_then(|open| open.clipboard.as_mut()) {
            Some(clipboard) => match clipboard.transfers.next().await {
                Some((mime, serial)) => Event::Transfer(mime, serial),
                None => Event::TransfersEnded,
            },
            None => future::pending().await,
        }
    };
    future::or(request, future::or(idle, transfer)).await
}

fn idle(minutes: u64) -> Option<Duration> {
    (minutes > 0).then(|| Duration::from_secs(minutes * 60))
}

impl State {
    fn remember(&mut self, failure: &Failure) {
        if failure.sticks() {
            self.stuck = Some(failure.clone());
        }
    }

    async fn ensure_open(&mut self) -> Result<&mut Open, Failure> {
        if let Some(failure) = &self.stuck {
            return Err(failure.clone());
        }
        if self.open.is_none() {
            match start().await {
                Ok(open) => self.open = Some(open),
                Err(failure) => {
                    self.remember(&failure);
                    return Err(failure);
                }
            }
        }
        Ok(self.open.as_mut().expect("opened above"))
    }

    async fn type_or_copy(&mut self, text: &str) -> Result<(), String> {
        let failure = match self.type_text(text).await {
            Ok(()) => return Ok(()),
            Err(failure) => failure,
        };
        eprintln!("[vinowhisper-tray] typing: {}", failure.message());
        if failure.sticks() {
            return Err(failure.message());
        }
        match self.copy(text.to_owned()).await {
            Ok(()) => Err(format!("{}; it is on the clipboard", failure.message())),
            Err(_) => Err(failure.message()),
        }
    }

    async fn type_text(&mut self, text: &str) -> Result<(), Failure> {
        let keysyms: Vec<i32> = text.chars().filter_map(keysym).collect();
        let Some((&first, rest)) = keysyms.split_first() else {
            return Ok(());
        };
        self.press_first_key(first).await?;
        let open = self.open.as_ref().expect("open after the first key");
        for &sym in rest {
            if let Err(failure) = press(open, sym).await {
                self.close_open().await;
                return Err(failure);
            }
        }
        Ok(())
    }

    // The desktop may have closed a kept session meanwhile. Retry once in a new one, and only
    // for the first key, so nothing is typed twice.
    async fn press_first_key(&mut self, sym: i32) -> Result<(), Failure> {
        let reused = self.open.is_some();
        let open = self.ensure_open().await?;
        let Err(failure) = press(open, sym).await else {
            return Ok(());
        };
        self.close_open().await;
        if !reused {
            return Err(failure);
        }
        press(self.ensure_open().await?, sym).await
    }

    async fn close_open(&mut self) {
        if let Some(open) = self.open.take() {
            open.close().await;
        }
    }

    async fn copy(&mut self, text: String) -> Result<(), Failure> {
        let open = self.ensure_open().await?;
        let Some(clipboard) = &open.clipboard else {
            return Err(Failure::Failed(
                "this desktop's remote-desktop portal has no clipboard".into(),
            ));
        };
        let mimes: Vec<&str> = MIME_TYPES.into_iter().chain([PASSWORD_HINT]).collect();
        bounded(
            CALL_TIMEOUT,
            "setting the clipboard",
            clipboard.proxy.set_selection(
                &open.session,
                SetSelectionOptions::default().set_mime_types(&mimes),
            ),
        )
        .await?;
        open.offer = Some(text);
        Ok(())
    }
}

async fn press(open: &Open, sym: i32) -> Result<(), Failure> {
    for state in [KeyState::Pressed, KeyState::Released] {
        bounded(
            CALL_TIMEOUT,
            "typing",
            open.portal
                .notify_keyboard_keysym(&open.session, sym, state, Default::default()),
        )
        .await?;
    }
    Ok(())
}

async fn serve(open: &Open, mime: &str, serial: u32) {
    let Some(PortalClipboard {
        proxy: clipboard, ..
    }) = &open.clipboard
    else {
        return;
    };
    let bytes = match &open.offer {
        Some(_) if mime == PASSWORD_HINT => b"secret".to_vec(),
        Some(text) => text.clone().into_bytes(),
        None => Vec::new(),
    };
    let written = match bounded(
        CALL_TIMEOUT,
        "handing over the text",
        clipboard.selection_write(&open.session, serial),
    )
    .await
    {
        // Small enough for a pipe buffer, so this does not block.
        Ok(fd) if !bytes.is_empty() => File::from(std::os::fd::OwnedFd::from(fd))
            .write_all(&bytes)
            .is_ok(),
        Ok(_) => false,
        Err(failure) => {
            eprintln!("[vinowhisper-tray] clipboard: {}", failure.message());
            false
        }
    };
    let _ = bounded(
        CALL_TIMEOUT,
        "finishing the hand-over",
        clipboard.selection_write_done(&open.session, serial, written),
    )
    .await;
}

async fn start() -> Result<Open, Failure> {
    let portal = bounded(CALL_TIMEOUT, "connecting", RemoteDesktop::new()).await?;
    let session = bounded(
        CALL_TIMEOUT,
        "creating a session",
        portal.create_session(Default::default()),
    )
    .await?;
    // Asked for before Start, or the portal refuses it.
    let clipboard = match request_clipboard(&session).await {
        Ok(clipboard) => Some(clipboard),
        Err(failure) => {
            eprintln!(
                "[vinowhisper-tray] no clipboard through the portal: {}",
                failure.message()
            );
            None
        }
    };
    let path = token_path();
    let token = std::fs::read_to_string(&path).ok();
    let options = SelectDevicesOptions::default()
        .set_devices(DeviceType::KEYBOARD)
        .set_persist_mode(PersistMode::ExplicitlyRevoked)
        .set_restore_token(token.as_deref().map(str::trim));
    let started = async {
        bounded(CALL_TIMEOUT, "selecting the keyboard", async {
            portal.select_devices(&session, options).await?.response()
        })
        .await?;
        bounded(PERMISSION_DIALOG_TIMEOUT, "starting", async {
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
            let _ = bounded(CALL_TIMEOUT, "closing the session", session.close()).await;
            return Err(failure);
        }
    };
    // A token is good for one start; the next one needs the replacement.
    if let Some(next) = started.restore_token() {
        save_token(&path, next);
    }
    if !started.devices().contains(DeviceType::KEYBOARD) {
        let _ = bounded(CALL_TIMEOUT, "closing the session", session.close()).await;
        return Err(Failure::Denied);
    }
    trace(format_args!(
        "portal: session started, keyboard {}, clipboard requested {}, enabled {}",
        started.devices().contains(DeviceType::KEYBOARD),
        clipboard.is_some(),
        started.is_clipboard_enabled()
    ));
    let clipboard = clipboard.filter(|_| started.is_clipboard_enabled());
    Ok(Open {
        portal,
        session,
        clipboard,
        offer: None,
    })
}

async fn request_clipboard(session: &Session<RemoteDesktop>) -> Result<PortalClipboard, Failure> {
    let clipboard = bounded(
        CALL_TIMEOUT,
        "connecting to the clipboard",
        Clipboard::new(),
    )
    .await?;
    bounded(
        CALL_TIMEOUT,
        "asking for the clipboard",
        clipboard.request(session, RequestClipboardOptions::default()),
    )
    .await?;
    // Straight from the proxy: ashpd's own stream borrows the Clipboard it came from.
    let signals = bounded(CALL_TIMEOUT, "listening for pastes", async {
        clipboard
            .receive_signal("SelectionTransfer")
            .await
            .map_err(ashpd::Error::from)
    })
    .await?;
    let transfers: Transfers = Box::pin(signals.filter_map(|message| {
        let (_, mime, serial): (ashpd::zbus::OwnedObjectPath, String, u32) =
            message.body().deserialize().ok()?;
        Some((mime, serial))
    }));
    Ok(PortalClipboard {
        proxy: clipboard,
        transfers,
    })
}

const SPACE_KEYSYM: i32 = 0x20;
const UNICODE_KEYSYM_BASE: u32 = 0x0100_0000;

// Keysyms, not keycodes, so the desktop's own layout picks the key for each character.
// (pinned by `tests::latin_1_is_its_own_keysym_and_the_rest_is_offset`)
fn keysym(c: char) -> Option<i32> {
    let code = c as u32;
    match c {
        // Never Return or Tab: a typed line break runs a terminal's command line.
        // (pinned by `tests::line_breaks_and_tabs_are_spaces_and_other_controls_are_dropped`)
        '\n' | '\t' => Some(SPACE_KEYSYM),
        _ if c.is_control() => None,
        _ if is_printable_latin_1(code) => Some(code as i32),
        _ => Some((UNICODE_KEYSYM_BASE + code) as i32),
    }
}

fn is_printable_latin_1(code: u32) -> bool {
    (0x20..0x7f).contains(&code) || (0xa0..0x100).contains(&code)
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
