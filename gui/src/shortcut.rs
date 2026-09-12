//! The global shortcut, through the XDG desktop portal.
//!
//! Wayland gives no client a way to grab a key while another window has
//! focus, and that is by design. The portal is the sanctioned route: this app
//! asks for a shortcut with a *preferred* trigger, the desktop decides (Plasma
//! asks the user once), and from then on the binding lives in the desktop's
//! own settings, where it can be changed like any other. "Change shortcut…"
//! in the tray opens exactly that page. Nothing here stores or parses a key
//! combination.
//!
//! All of it is best-effort. Without the portal (an older desktop, or a
//! compositor that does not implement GlobalShortcuts), `vinowhisper-gui
//! toggle` does the same job from any keybinding tool.

use std::thread;

use ashpd::desktop::global_shortcuts::{GlobalShortcuts, NewShortcut, Shortcut as Bound};
use futures_lite::{StreamExt, future};
use smithay_client_toolkit::reexports::calloop::channel::Sender;

use crate::APP_ID;
use crate::app::Command;

/// The id the desktop stores the binding under. Renaming it orphans every
/// binding anyone has already made.
const TOGGLE: &str = "toggle-captions";

#[derive(Debug, Clone, PartialEq)]
pub enum State {
    Pending,
    Bound {
        /// How the desktop describes the trigger ("Meta+Alt+C"); empty when
        /// the user declined to bind one.
        trigger: String,
        /// ConfigureShortcuts only exists from portal version 2.
        configurable: bool,
    },
    Unavailable(String),
}

pub struct Shortcut {
    configure: async_channel::Sender<()>,
}

impl Shortcut {
    pub fn spawn(preferred: String, tx: Sender<Command>) -> Shortcut {
        let (configure, requests) = async_channel::unbounded();
        let spawned = thread::Builder::new()
            .name("portal-shortcut".into())
            .spawn({
                let tx = tx.clone();
                move || {
                    if let Err(err) = future::block_on(run(&preferred, &tx, requests)) {
                        eprintln!(
                            "[vinowhisper-gui] no global shortcut ({err}). Bind \
                             `vinowhisper-gui toggle` in your desktop's keyboard settings instead."
                        );
                        let _ = tx.send(Command::Shortcut(State::Unavailable(err.to_string())));
                    }
                }
            });
        if let Err(err) = spawned {
            let _ = tx.send(Command::Shortcut(State::Unavailable(err.to_string())));
        }
        Shortcut { configure }
    }

    /// Open the desktop's own page for this app's shortcuts.
    pub fn configure(&self) {
        let _ = self.configure.try_send(());
    }
}

enum Wake {
    Activated(bool),
    Changed(Option<Vec<Bound>>),
    Configure,
    Done,
}

async fn run(
    preferred: &str,
    tx: &Sender<Command>,
    requests: async_channel::Receiver<()>,
) -> ashpd::Result<()> {
    // A host app, unlike a Flatpak, has no app id the portal can see, and
    // Plasma's GlobalShortcuts refuses a client without one ("An app id is
    // required", measured 2026-09-12). Registering ties this process to the
    // desktop file install::ensure_launcher wrote a moment ago. A portal older
    // than the registry refuses the call; carry on regardless, since some
    // desktops grant shortcuts without an id.
    if let Err(err) = ashpd::register_host_app(APP_ID.try_into()?).await {
        eprintln!("[vinowhisper-gui] portal registry: {err} (continuing without an app id)");
    }

    let portal = GlobalShortcuts::new().await?;
    let configurable = portal.version() >= 2;
    let session = portal.create_session(Default::default()).await?;
    let mut activated = portal.receive_activated().await?;
    let mut changed = portal.receive_shortcuts_changed().await?;

    let request =
        NewShortcut::new(TOGGLE, "Show or hide live captions").preferred_trigger(preferred);
    let bound = portal
        .bind_shortcuts(&session, &[request], None, Default::default())
        .await?
        .response()?;
    report(tx, bound.shortcuts(), configurable);

    loop {
        let wake = future::or(
            future::or(
                async {
                    match activated.next().await {
                        Some(event) => Wake::Activated(event.shortcut_id() == TOGGLE),
                        None => Wake::Done,
                    }
                },
                async {
                    Wake::Changed(changed.next().await.map(|event| event.shortcuts().to_vec()))
                },
            ),
            async {
                match requests.recv().await {
                    Ok(()) => Wake::Configure,
                    Err(_) => Wake::Done,
                }
            },
        )
        .await;

        match wake {
            Wake::Activated(true) => {
                let _ = tx.send(Command::Toggle);
            }
            Wake::Activated(false) => {}
            Wake::Changed(Some(shortcuts)) => report(tx, &shortcuts, configurable),
            Wake::Configure if configurable => {
                if let Err(err) = portal
                    .configure_shortcuts(&session, None, Default::default())
                    .await
                {
                    eprintln!("[vinowhisper-gui] could not open the shortcut settings: {err}");
                }
            }
            Wake::Configure => {}
            Wake::Changed(None) | Wake::Done => return Ok(()),
        }
    }
}

fn report(tx: &Sender<Command>, shortcuts: &[Bound], configurable: bool) {
    let trigger = shortcuts
        .iter()
        .find(|shortcut| shortcut.id() == TOGGLE)
        .map(|shortcut| shortcut.trigger_description().to_owned())
        .unwrap_or_default();
    let _ = tx.send(Command::Shortcut(State::Bound {
        trigger,
        configurable,
    }));
}
