use std::thread;

use ashpd::desktop::global_shortcuts::{GlobalShortcuts, NewShortcut, Shortcut as Bound};
use futures_lite::{StreamExt, future};
use smithay_client_toolkit::reexports::calloop::channel::Sender;

use crate::APP_ID;
use crate::app::Command;

/// Renaming either orphans every binding already made.
const TOGGLE: &str = "toggle-captions";
const DICTATE: &str = "dictate";

/// The dictation key on laptops that have one sends Meta+H, after Windows' Win+H.
pub const DICTATE_PREFERRED: &str = "LOGO+h";

#[derive(Debug, Clone, PartialEq)]
pub enum State {
    Pending,
    Bound {
        trigger: String,
        dictate: String,
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

    pub fn configure(&self) {
        let _ = self.configure.try_send(());
    }
}

enum Wake {
    Activated(String),
    Deactivated(String),
    Changed(Option<Vec<Bound>>),
    Configure,
    Done,
}

async fn run(
    preferred: &str,
    tx: &Sender<Command>,
    requests: async_channel::Receiver<()>,
) -> ashpd::Result<()> {
    if let Err(err) = ashpd::register_host_app(APP_ID.try_into()?).await {
        eprintln!("[vinowhisper-gui] portal registry: {err} (continuing without an app id)");
    }

    let portal = GlobalShortcuts::new().await?;
    let configurable = portal.version() >= 2;
    let session = portal.create_session(Default::default()).await?;
    let mut activated = portal.receive_activated().await?;
    let mut deactivated = portal.receive_deactivated().await?;
    let mut changed = portal.receive_shortcuts_changed().await?;

    let wanted = [
        NewShortcut::new(TOGGLE, "Show or hide live captions").preferred_trigger(preferred),
        NewShortcut::new(DICTATE, "Dictate: hold to talk, or tap to start and stop")
            .preferred_trigger(DICTATE_PREFERRED),
    ];
    let bound = portal
        .bind_shortcuts(&session, &wanted, None, Default::default())
        .await?
        .response()?;
    report(tx, bound.shortcuts(), configurable);

    loop {
        let wake = future::or(
            future::or(
                future::or(
                    async {
                        match activated.next().await {
                            Some(event) => Wake::Activated(event.shortcut_id().to_owned()),
                            None => Wake::Done,
                        }
                    },
                    async {
                        match deactivated.next().await {
                            Some(event) => Wake::Deactivated(event.shortcut_id().to_owned()),
                            None => Wake::Done,
                        }
                    },
                ),
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
            Wake::Activated(id) if id == TOGGLE => {
                let _ = tx.send(Command::Toggle);
            }
            Wake::Activated(id) if id == DICTATE => {
                let _ = tx.send(Command::DictateKey { down: true });
            }
            Wake::Deactivated(id) if id == DICTATE => {
                let _ = tx.send(Command::DictateKey { down: false });
            }
            Wake::Activated(_) | Wake::Deactivated(_) => {}
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
    let trigger = |id: &str| {
        shortcuts
            .iter()
            .find(|shortcut| shortcut.id() == id)
            .map(|shortcut| shortcut.trigger_description().to_owned())
            .unwrap_or_default()
    };
    let _ = tx.send(Command::Shortcut(State::Bound {
        trigger: trigger(TOGGLE),
        dictate: trigger(DICTATE),
        configurable,
    }));
}
