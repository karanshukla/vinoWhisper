use std::sync::mpsc::{self, Receiver, Sender};
use std::time::Duration;

use zbus::zvariant::OwnedValue;

const PREFIX: &str = "org.mpris.MediaPlayer2.";
const PATH: &str = "/org/mpris/MediaPlayer2";
const PLAYER: &str = "org.mpris.MediaPlayer2.Player";
// A player that does not answer must not hold the next one up, or the resume.
const CALL_TIMEOUT: Duration = Duration::from_secs(1);
const FINISH_TIMEOUT: Duration = Duration::from_secs(2);

pub trait Players {
    fn names(&mut self) -> Vec<String>;
    fn status(&mut self, name: &str) -> Option<String>;
    fn call(&mut self, name: &str, method: &str) -> bool;
}

pub fn pause_playing(players: &mut impl Players, held: &mut Vec<String>) {
    for name in players.names() {
        if !name.starts_with(PREFIX) || held.contains(&name) {
            continue;
        }
        if players.status(&name).as_deref() == Some("Playing") && players.call(&name, "Pause") {
            held.push(name);
        }
    }
}

pub fn resume_paused(players: &mut impl Players, held: &mut Vec<String>) {
    for name in held.drain(..) {
        // Anything else and the user (or the player) moved on during the dictation.
        if players.status(&name).as_deref() == Some("Paused") {
            players.call(&name, "Play");
        }
    }
}

enum Request {
    Pause,
    Resume(Option<Sender<()>>),
}

#[derive(Debug)]
pub struct Media {
    tx: Option<Sender<Request>>,
    held: bool,
}

impl Media {
    pub fn new() -> Self {
        Media {
            tx: None,
            held: false,
        }
    }

    pub fn pause(&mut self) {
        self.held = self.send(Request::Pause);
    }

    pub fn resume(&mut self) {
        if std::mem::take(&mut self.held) {
            self.send(Request::Resume(None));
        }
    }

    // On the way out: the process must not exit with the user's music still paused.
    pub fn finish(&mut self) {
        if std::mem::take(&mut self.held) {
            let (done, wait) = mpsc::channel();
            if self.send(Request::Resume(Some(done))) {
                let _ = wait.recv_timeout(FINISH_TIMEOUT);
            }
        }
    }

    fn send(&mut self, request: Request) -> bool {
        if self.tx.is_none() {
            let (tx, rx) = mpsc::channel();
            let spawned = std::thread::Builder::new()
                .name("media".into())
                .spawn(move || serve(rx));
            match spawned {
                Ok(_) => self.tx = Some(tx),
                Err(err) => {
                    eprintln!("[vinowhisper-tray] could not start the media thread: {err}");
                    return false;
                }
            }
        }
        self.tx.as_ref().is_some_and(|tx| tx.send(request).is_ok())
    }
}

// One thread, so a resume can never overtake the pause it undoes.
fn serve(requests: Receiver<Request>) {
    let mut held = Vec::new();
    for request in requests {
        let mut bus = match Bus::connect() {
            Ok(bus) => bus,
            Err(err) => {
                eprintln!("[vinowhisper-tray] no session bus to pause media on: {err}");
                continue;
            }
        };
        match request {
            Request::Pause => pause_playing(&mut bus, &mut held),
            Request::Resume(done) => {
                resume_paused(&mut bus, &mut held);
                if let Some(done) = done {
                    let _ = done.send(());
                }
            }
        }
    }
}

struct Bus(zbus::Connection);

impl Bus {
    fn connect() -> zbus::Result<Bus> {
        async_io::block_on(async {
            let connection = zbus::connection::Builder::session()?
                .method_timeout(CALL_TIMEOUT)
                .build()
                .await?;
            Ok(Bus(connection))
        })
    }
}

impl Players for Bus {
    fn names(&mut self) -> Vec<String> {
        async_io::block_on(self.0.call_method(
            Some("org.freedesktop.DBus"),
            "/org/freedesktop/DBus",
            Some("org.freedesktop.DBus"),
            "ListNames",
            &(),
        ))
        .ok()
        .and_then(|reply| reply.body().deserialize::<Vec<String>>().ok())
        .unwrap_or_default()
    }

    fn status(&mut self, name: &str) -> Option<String> {
        let reply = async_io::block_on(self.0.call_method(
            Some(name),
            PATH,
            Some("org.freedesktop.DBus.Properties"),
            "Get",
            &(PLAYER, "PlaybackStatus"),
        ))
        .ok()?;
        let value = reply.body().deserialize::<OwnedValue>().ok()?;
        String::try_from(value).ok()
    }

    fn call(&mut self, name: &str, method: &str) -> bool {
        async_io::block_on(
            self.0
                .call_method(Some(name), PATH, Some(PLAYER), method, &()),
        )
        .is_ok()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[derive(Default)]
    struct Fake {
        players: Vec<(String, Option<&'static str>)>,
        refuses: Vec<&'static str>,
        calls: Vec<(String, String)>,
    }

    impl Fake {
        fn with(players: &[(&str, &'static str)]) -> Fake {
            Fake {
                players: players
                    .iter()
                    .map(|(name, status)| (format!("{PREFIX}{name}"), Some(*status)))
                    .collect(),
                ..Fake::default()
            }
        }

        fn set(&mut self, player: &str, status: Option<&'static str>) {
            let name = format!("{PREFIX}{player}");
            self.players.iter_mut().find(|(n, _)| *n == name).unwrap().1 = status;
        }

        fn called(&self) -> Vec<(&str, &str)> {
            self.calls
                .iter()
                .map(|(name, method)| (name.trim_start_matches(PREFIX), method.as_str()))
                .collect()
        }
    }

    impl Players for Fake {
        fn names(&mut self) -> Vec<String> {
            let mut names = vec![
                "org.freedesktop.Notifications".to_owned(),
                ":1.42".to_owned(),
            ];
            names.extend(self.players.iter().map(|(name, _)| name.clone()));
            names
        }

        fn status(&mut self, name: &str) -> Option<String> {
            let (_, status) = self.players.iter().find(|(n, _)| n == name)?;
            status.map(str::to_owned)
        }

        fn call(&mut self, name: &str, method: &str) -> bool {
            self.calls.push((name.to_owned(), method.to_owned()));
            if self.refuses.iter().any(|r| name.ends_with(r)) {
                return false;
            }
            let status = match method {
                "Pause" => "Paused",
                _ => "Playing",
            };
            if let Some(player) = self.players.iter_mut().find(|(n, _)| n == name) {
                player.1 = Some(status);
            }
            true
        }
    }

    #[test]
    fn only_players_that_are_playing_are_paused_and_only_those_resume() {
        let mut bus = Fake::with(&[
            ("vlc", "Playing"),
            ("mpv", "Paused"),
            ("spotify", "Stopped"),
        ]);
        let mut held = Vec::new();
        pause_playing(&mut bus, &mut held);
        assert_eq!(bus.called(), [("vlc", "Pause")]);

        bus.calls.clear();
        resume_paused(&mut bus, &mut held);
        // mpv was paused before the dictation and stays that way.
        assert_eq!(bus.called(), [("vlc", "Play")]);
        assert!(held.is_empty());
    }

    #[test]
    fn a_player_the_user_touched_meanwhile_is_left_alone() {
        let mut bus = Fake::with(&[
            ("vlc", "Playing"),
            ("firefox", "Playing"),
            ("mpv", "Playing"),
        ]);
        let mut held = Vec::new();
        pause_playing(&mut bus, &mut held);
        bus.set("vlc", Some("Stopped"));
        bus.set("firefox", Some("Playing"));
        bus.set("mpv", None);

        bus.calls.clear();
        resume_paused(&mut bus, &mut held);
        assert_eq!(bus.called(), []);
        assert!(held.is_empty());
    }

    #[test]
    fn a_player_that_refuses_the_pause_is_not_resumed() {
        let mut bus = Fake::with(&[("vlc", "Playing"), ("mpv", "Playing")]);
        bus.refuses = vec!["vlc"];
        let mut held = Vec::new();
        pause_playing(&mut bus, &mut held);
        assert_eq!(held, [format!("{PREFIX}mpv")]);
    }

    #[test]
    fn a_second_dictation_before_the_resume_keeps_the_first_list() {
        let mut bus = Fake::with(&[("vlc", "Playing"), ("mpv", "Stopped")]);
        let mut held = Vec::new();
        pause_playing(&mut bus, &mut held);
        bus.set("mpv", Some("Playing"));
        pause_playing(&mut bus, &mut held);
        assert_eq!(bus.called(), [("vlc", "Pause"), ("mpv", "Pause")]);

        bus.calls.clear();
        resume_paused(&mut bus, &mut held);
        assert_eq!(bus.called(), [("vlc", "Play"), ("mpv", "Play")]);
    }

    #[test]
    fn nothing_is_sent_to_the_bus_until_something_was_paused() {
        let mut media = Media::new();
        media.resume();
        media.finish();
        assert!(media.tx.is_none());
    }
}
