use std::collections::VecDeque;
use std::fmt;
use std::time::{Duration, Instant};

use crate::captions::Tone;
use crate::protocol::Dictate;

pub const TAP: Duration = Duration::from_millis(350);

const PREVIEW_CHARS: usize = 64;
pub const RECENT: usize = 5;

#[derive(Clone, PartialEq)]
pub enum Phase {
    Idle,
    Listening {
        hands_free: bool,
        live: bool,
        text: String,
    },
    Transcribing,
    Typed {
        text: String,
        pasted: Option<Result<(), String>>,
    },
    Nothing,
    Failed(String),
}

impl fmt::Debug for Phase {
    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result {
        match self {
            Phase::Idle => f.write_str("Idle"),
            Phase::Listening {
                hands_free,
                live,
                text,
            } => f
                .debug_struct("Listening")
                .field("hands_free", hands_free)
                .field("live", live)
                .field("chars", &text.chars().count())
                .finish(),
            Phase::Transcribing => f.write_str("Transcribing"),
            Phase::Typed { text, pasted } => f
                .debug_struct("Typed")
                .field("chars", &text.chars().count())
                .field("pasted", pasted)
                .finish(),
            Phase::Nothing => f.write_str("Nothing"),
            Phase::Failed(message) => f.debug_tuple("Failed").field(message).finish(),
        }
    }
}

#[derive(Clone, PartialEq)]
pub enum Action {
    Start,
    HandsFree,
    Stop,
    Paste(String),
}

impl fmt::Debug for Action {
    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result {
        match self {
            Action::Start => f.write_str("Start"),
            Action::HandsFree => f.write_str("HandsFree"),
            Action::Stop => f.write_str("Stop"),
            Action::Paste(text) => write!(f, "Paste({} chars)", text.chars().count()),
        }
    }
}

#[derive(Clone, PartialEq, Default)]
pub struct Recent {
    entries: VecDeque<(u64, String)>,
    next: u64,
}

impl fmt::Debug for Recent {
    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result {
        write!(f, "Recent({} entries)", self.entries.len())
    }
}

impl Recent {
    pub fn push(&mut self, text: &str) {
        let text = text.trim();
        if text.is_empty() {
            return;
        }
        self.entries.push_front((self.next, text.to_owned()));
        self.next += 1;
        self.entries.truncate(RECENT);
    }

    pub fn items(&self) -> impl Iterator<Item = (u64, &str)> {
        self.entries.iter().map(|(id, text)| (*id, text.as_str()))
    }

    pub fn get(&self, id: u64) -> Option<&str> {
        self.items()
            .find(|(held, _)| *held == id)
            .map(|(_, text)| text)
    }

    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }

    pub fn clear(&mut self) {
        self.entries.clear();
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct Pill {
    pub tone: Tone,
    pub text: String,
    pub level: Option<f32>,
    pub status: bool,
}

#[derive(Debug)]
pub struct Dictation {
    phase: Phase,
    key_down_at: Option<Instant>,
    swallow_release: bool,
    level: f32,
    device: Option<(String, bool)>,
    epoch: u64,
    notice: Option<(u64, String)>,
    notices: u64,
    cleaning: bool,
    announcement: Option<String>,
}

impl Dictation {
    pub fn new() -> Self {
        Dictation {
            phase: Phase::Idle,
            key_down_at: None,
            swallow_release: false,
            level: 0.0,
            device: None,
            epoch: 0,
            notice: None,
            notices: 0,
            cleaning: false,
            announcement: None,
        }
    }

    pub fn phase(&self) -> &Phase {
        &self.phase
    }

    // For the tray's tooltip. Never the dictated words: those stay on the pill, off D-Bus.
    pub fn status(&self) -> Option<String> {
        let text = match &self.phase {
            Phase::Idle => return None,
            Phase::Listening { live: false, .. } => "starting the microphone".to_owned(),
            Phase::Listening {
                hands_free: true, ..
            } => "recording, hands-free (tap the key to finish)".to_owned(),
            Phase::Listening { .. } => "recording (release the key to finish)".to_owned(),
            Phase::Transcribing => "transcribing".to_owned(),
            Phase::Typed { pasted: None, .. } => "typing".to_owned(),
            Phase::Typed {
                pasted: Some(Ok(())),
                ..
            } => "typed".to_owned(),
            Phase::Typed {
                pasted: Some(Err(why)),
                ..
            } => format!("on the clipboard, not typed: {why}"),
            Phase::Nothing => "heard nothing".to_owned(),
            Phase::Failed(message) => format!("failed: {message}"),
        };
        Some(format!("Dictation {text}"))
    }

    pub fn epoch(&self) -> u64 {
        self.epoch
    }

    /// Recording, waiting on the server, or holding text not yet pasted: anything that a
    /// server restart would lose.
    pub fn is_busy(&self) -> bool {
        matches!(
            self.phase,
            Phase::Listening { .. } | Phase::Transcribing | Phase::Typed { pasted: None, .. }
        )
    }

    /// A refusal shown on the pill in place of its text, so it never disturbs the phase.
    /// Returns an id for `clear_notice`, so a stale timer cannot remove a newer notice.
    pub fn notice(&mut self, message: impl Into<String>) -> u64 {
        self.notices += 1;
        self.notice = Some((self.notices, message.into()));
        self.notices
    }

    pub fn clear_notice(&mut self, id: u64) {
        if self.notice.as_ref().is_some_and(|(held, _)| *held == id) {
            self.notice = None;
        }
    }

    /// What a screen reader should hear about the last change, once: failures and the
    /// clipboard fallback, never the words or the routine states.
    pub fn take_announcement(&mut self) -> Option<String> {
        self.announcement.take()
    }

    fn set(&mut self, phase: Phase) {
        if phase != self.phase {
            match &phase {
                Phase::Failed(message) => self.announcement = Some(message.clone()),
                Phase::Typed {
                    pasted: Some(Err(why)),
                    ..
                } => self.announcement = Some(clipboard_fallback(why)),
                _ => {}
            }
            self.phase = phase;
            self.cleaning = false;
            self.epoch += 1;
        }
    }

    pub fn key(&mut self, down: bool, now: Instant) -> Option<Action> {
        if down {
            if self.key_down_at.is_some() {
                return None;
            }
            self.key_down_at = Some(now);
            match self.phase {
                Phase::Listening {
                    hands_free: true, ..
                } => {
                    self.swallow_release = true;
                    self.set(Phase::Transcribing);
                    Some(Action::Stop)
                }
                Phase::Listening { .. } | Phase::Transcribing => {
                    self.swallow_release = true;
                    None
                }
                _ => {
                    self.level = 0.0;
                    self.set(Phase::Listening {
                        hands_free: false,
                        live: false,
                        text: String::new(),
                    });
                    Some(Action::Start)
                }
            }
        } else {
            let since = self.key_down_at.take()?;
            if std::mem::take(&mut self.swallow_release) {
                return None;
            }
            let Phase::Listening {
                hands_free: false,
                live,
                text,
            } = self.phase.clone()
            else {
                return None;
            };
            if now.duration_since(since) < TAP {
                self.set(Phase::Listening {
                    hands_free: true,
                    live,
                    text,
                });
                Some(Action::HandsFree)
            } else {
                self.set(Phase::Transcribing);
                Some(Action::Stop)
            }
        }
    }

    pub fn event(&mut self, event: Dictate) -> Option<Action> {
        match event {
            Dictate::Listening => {
                if let Phase::Listening {
                    hands_free, text, ..
                } = self.phase.clone()
                {
                    self.set(Phase::Listening {
                        hands_free,
                        live: true,
                        text,
                    });
                }
            }
            Dictate::Partial { text } => {
                if let Phase::Listening {
                    hands_free, live, ..
                } = self.phase
                {
                    self.set(Phase::Listening {
                        hands_free,
                        live,
                        text: text.trim().to_owned(),
                    });
                }
            }
            Dictate::Level { rms } => self.level = rms,
            Dictate::Transcribing => {
                if self.key_down_at.is_some() {
                    self.swallow_release = true;
                }
                self.set(Phase::Transcribing);
            }
            Dictate::Cleaning => {
                if self.phase == Phase::Transcribing && !self.cleaning {
                    self.cleaning = true;
                    self.epoch += 1;
                }
            }
            Dictate::Ready { device, degraded } => self.device = Some((device, degraded)),
            Dictate::Dictated { text } => {
                let text = text.trim().to_owned();
                if text.is_empty() {
                    self.set(Phase::Nothing);
                } else {
                    self.set(Phase::Typed {
                        text: text.clone(),
                        pasted: None,
                    });
                    return Some(Action::Paste(text));
                }
            }
            Dictate::Notice { message } => {
                self.announcement = Some(message.clone());
                self.notice(message);
                // A new epoch, so the pill lingers long enough to read it even if the
                // paste already finished and armed the short timer.
                self.epoch += 1;
            }
            Dictate::Cancelled => self.set(Phase::Idle),
            Dictate::Error { message } => self.set(Phase::Failed(message)),
        }
        None
    }

    pub fn pasted(&mut self, result: Result<(), String>) {
        if let Phase::Typed { text, pasted: None } = &self.phase {
            let text = text.clone();
            self.set(Phase::Typed {
                text,
                pasted: Some(result),
            });
        }
    }

    pub fn fail(&mut self, message: impl Into<String>) {
        self.set(Phase::Failed(message.into()));
    }

    pub fn child_exited(&mut self, message: String) {
        if matches!(self.phase, Phase::Listening { .. } | Phase::Transcribing) {
            self.set(Phase::Failed(message));
        }
    }

    pub fn dismiss(&mut self, epoch: u64) {
        if epoch == self.epoch {
            self.notice = None;
            self.set(Phase::Idle);
        }
    }

    pub fn linger(&self) -> Option<Duration> {
        let done = matches!(
            self.phase,
            Phase::Typed {
                pasted: Some(Ok(())),
                ..
            } | Phase::Nothing
        );
        if done && self.notice.is_some() {
            return Some(Duration::from_secs(6));
        }
        match &self.phase {
            Phase::Typed { pasted: None, .. } => None,
            Phase::Typed {
                pasted: Some(Ok(())),
                ..
            }
            | Phase::Nothing => Some(Duration::from_millis(1500)),
            Phase::Typed {
                pasted: Some(Err(_)),
                ..
            } => Some(Duration::from_secs(5)),
            Phase::Failed(_) => Some(Duration::from_secs(6)),
            Phase::Idle | Phase::Listening { .. } | Phase::Transcribing => None,
        }
    }

    pub fn pill(&self) -> Option<Pill> {
        let (tone, text, level) = match &self.phase {
            Phase::Idle => return None,
            Phase::Listening { live: false, .. } => {
                (Tone::Dim, "Starting the microphone".to_owned(), None)
            }
            Phase::Listening { text, .. } => (Tone::Good, preview(text), Some(self.level)),
            Phase::Transcribing => (Tone::Warn, self.transcribing_text(), None),
            Phase::Typed {
                text,
                pasted: None | Some(Ok(())),
            } => (Tone::Good, preview(text), None),
            Phase::Typed {
                pasted: Some(Err(why)),
                ..
            } => (Tone::Warn, clipboard_fallback(why), None),
            Phase::Nothing => (Tone::Dim, "Heard nothing".to_owned(), None),
            Phase::Failed(message) => (Tone::Bad, message.clone(), None),
        };
        let words = matches!(
            self.phase,
            Phase::Listening { live: true, .. }
                | Phase::Typed {
                    pasted: None | Some(Ok(())),
                    ..
                }
        );
        match &self.notice {
            Some((_, message)) => Some(Pill {
                tone: Tone::Warn,
                text: message.clone(),
                level,
                status: true,
            }),
            None => Some(Pill {
                tone,
                text,
                level,
                status: !words,
            }),
        }
    }

    fn transcribing_text(&self) -> String {
        if self.cleaning {
            return "Cleaning up with the model".to_owned();
        }
        match &self.device {
            Some((device, true)) => format!("Transcribing on {device}"),
            _ => "Transcribing".to_owned(),
        }
    }
}

fn clipboard_fallback(why: &str) -> String {
    format!("On the clipboard, not typed: {why}")
}

fn preview(text: &str) -> String {
    let count = text.chars().count();
    if count <= PREVIEW_CHARS {
        return text.to_owned();
    }
    let tail: String = text.chars().skip(count - (PREVIEW_CHARS - 1)).collect();
    format!("…{tail}")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn at(ms: u64, base: Instant) -> Instant {
        base + Duration::from_millis(ms)
    }

    #[test]
    fn each_phase_has_its_own_tray_text_and_none_carries_the_words() {
        let words = "the private words";
        let phases = [
            Phase::Listening {
                hands_free: false,
                live: false,
                text: String::new(),
            },
            Phase::Listening {
                hands_free: false,
                live: true,
                text: words.into(),
            },
            Phase::Listening {
                hands_free: true,
                live: true,
                text: words.into(),
            },
            Phase::Transcribing,
            Phase::Typed {
                text: words.into(),
                pasted: None,
            },
            Phase::Typed {
                text: words.into(),
                pasted: Some(Ok(())),
            },
            Phase::Typed {
                text: words.into(),
                pasted: Some(Err("no keyboard".into())),
            },
            Phase::Nothing,
            Phase::Failed("the server stopped".into()),
        ];
        let mut d = Dictation::new();
        assert_eq!(d.status(), None, "idle says nothing");
        let mut seen = std::collections::HashSet::new();
        for phase in phases {
            d.set(phase);
            let status = d.status().expect("a busy phase is described");
            assert!(status.starts_with("Dictation "), "{status}");
            assert!(!status.contains("private"), "{status}");
            assert!(seen.insert(status.clone()), "repeated: {status}");
        }
        assert!(seen.iter().any(|s| s.contains("recording")));
        assert!(
            seen.iter()
                .any(|s| s.contains("failed: the server stopped"))
        );
    }

    #[test]
    fn dictation_is_busy_from_the_key_press_until_the_text_is_pasted() {
        let t = Instant::now();
        let mut d = Dictation::new();
        assert!(!d.is_busy());
        d.key(true, t);
        d.event(Dictate::Listening);
        assert!(d.is_busy());
        d.key(false, at(500, t));
        assert!(d.is_busy(), "transcribing");
        d.event(Dictate::Dictated {
            text: "hello".into(),
        });
        assert!(d.is_busy(), "typed, not yet pasted");
        d.pasted(Ok(()));
        assert!(!d.is_busy());
    }

    #[test]
    fn a_notice_replaces_the_pill_text_without_changing_the_phase() {
        let t = Instant::now();
        let mut d = Dictation::new();
        d.key(true, t);
        d.event(Dictate::Listening);
        let id = d.notice("Finish dictating first");
        let pill = d.pill().unwrap();
        assert_eq!(pill.text, "Finish dictating first");
        assert_eq!(pill.tone, Tone::Warn);
        assert!(matches!(d.phase(), Phase::Listening { .. }));
        d.clear_notice(id);
        assert_eq!(d.pill().unwrap().text, "");
    }

    #[test]
    fn a_notice_after_the_text_holds_the_pill_until_it_is_dismissed() {
        let t = Instant::now();
        let mut d = Dictation::new();
        d.key(true, t);
        d.event(Dictate::Listening);
        d.key(false, at(900, t));
        d.event(Dictate::Dictated { text: "Hi".into() });
        d.pasted(Ok(()));
        assert_eq!(d.linger(), Some(Duration::from_millis(1500)));
        let typed = d.epoch();
        d.event(Dictate::Notice {
            message:
                "LLM cleanup off: the cleanup model isn't downloaded. Download it in Settings."
                    .into(),
        });
        assert_ne!(
            d.epoch(),
            typed,
            "a fresh timer, not the 1.5s one already armed"
        );
        assert_eq!(d.linger(), Some(Duration::from_secs(6)));
        let pill = d.pill().unwrap();
        assert!(pill.text.starts_with("LLM cleanup off"));
        assert!(pill.status, "a status, not the words");
        d.dismiss(typed);
        assert!(d.pill().is_some(), "the stale timer leaves it up");
        d.dismiss(d.epoch());
        assert_eq!(d.pill(), None);
        assert_eq!(d.linger(), None);
    }

    #[test]
    fn failures_and_warnings_are_announced_once_and_never_the_words() {
        let t = Instant::now();
        let mut d = Dictation::new();
        d.key(true, t);
        d.event(Dictate::Listening);
        d.event(Dictate::Partial {
            text: "the private words".into(),
        });
        assert_eq!(d.take_announcement(), None, "recording is not news");
        d.key(false, at(900, t));
        d.event(Dictate::Dictated {
            text: "the private words".into(),
        });
        assert_eq!(d.take_announcement(), None, "typed text stays on the pill");
        d.pasted(Err("no keyboard".into()));
        assert_eq!(
            d.take_announcement().as_deref(),
            Some("On the clipboard, not typed: no keyboard")
        );
        assert_eq!(d.take_announcement(), None, "once");
        d.event(Dictate::Notice {
            message: "LLM cleanup paused for 10 min (timed out)".into(),
        });
        assert_eq!(
            d.take_announcement().as_deref(),
            Some("LLM cleanup paused for 10 min (timed out)")
        );
        d.event(Dictate::Error {
            message: "Microphone capture failed".into(),
        });
        assert_eq!(
            d.take_announcement().as_deref(),
            Some("Microphone capture failed")
        );
    }

    #[test]
    fn a_refusal_flashed_on_the_pill_is_not_announced() {
        let mut d = Dictation::new();
        d.notice("Finish dictating first");
        assert_eq!(d.take_announcement(), None);
    }

    #[test]
    fn a_stale_timer_does_not_clear_a_newer_notice() {
        let mut d = Dictation::new();
        let old = d.notice("one");
        let new = d.notice("two");
        d.clear_notice(old);
        assert!(d.notice.is_some());
        d.clear_notice(new);
        assert!(d.notice.is_none());
    }

    #[test]
    fn hold_to_talk_starts_on_press_and_stops_on_release() {
        let t = Instant::now();
        let mut d = Dictation::new();
        assert_eq!(d.key(true, t), Some(Action::Start));
        d.event(Dictate::Listening);
        assert_eq!(d.key(false, at(2000, t)), Some(Action::Stop));
        assert_eq!(d.phase(), &Phase::Transcribing);
    }

    #[test]
    fn a_tap_latches_hands_free_and_the_next_press_stops_it() {
        let t = Instant::now();
        let mut d = Dictation::new();
        assert_eq!(d.key(true, t), Some(Action::Start));
        assert_eq!(d.key(false, at(60, t)), Some(Action::HandsFree));
        assert!(matches!(
            d.phase(),
            Phase::Listening {
                hands_free: true,
                ..
            }
        ));
        assert_eq!(d.key(true, at(5000, t)), Some(Action::Stop));
        assert_eq!(
            d.key(false, at(5060, t)),
            None,
            "the stopping tap's release is not a second tap"
        );
        assert_eq!(d.phase(), &Phase::Transcribing);
    }

    #[test]
    fn a_press_just_under_the_tap_length_latches_hands_free() {
        let t = Instant::now();
        let mut d = Dictation::new();
        d.key(true, t);
        assert_eq!(
            d.key(false, t + TAP - Duration::from_millis(1)),
            Some(Action::HandsFree)
        );
        assert!(matches!(
            d.phase(),
            Phase::Listening {
                hands_free: true,
                ..
            }
        ));
    }

    #[test]
    fn a_press_of_the_tap_length_is_hold_to_talk() {
        let t = Instant::now();
        let mut d = Dictation::new();
        d.key(true, t);
        assert_eq!(d.key(false, t + TAP), Some(Action::Stop));
    }

    #[test]
    fn key_repeat_and_presses_while_transcribing_do_nothing() {
        let t = Instant::now();
        let mut d = Dictation::new();
        d.key(true, t);
        assert_eq!(d.key(true, at(500, t)), None, "a repeat while held");
        d.key(false, at(1000, t));
        assert_eq!(d.key(true, at(1100, t)), None);
        assert_eq!(d.key(false, at(1200, t)), None);
        assert_eq!(d.phase(), &Phase::Transcribing);
    }

    #[test]
    fn cleaning_says_the_text_is_with_the_model_and_ends_with_the_result() {
        let mut d = Dictation::new();
        d.key(true, Instant::now());
        d.event(Dictate::Transcribing);
        let before = d.epoch();
        assert_eq!(d.pill().unwrap().text, "Transcribing");
        d.event(Dictate::Cleaning);
        assert_eq!(d.phase(), &Phase::Transcribing);
        assert_eq!(d.pill().unwrap().text, "Cleaning up with the model");
        assert!(d.epoch() > before);
        d.event(Dictate::Dictated {
            text: "Hello.".into(),
        });
        assert_eq!(d.pill().unwrap().text, "Hello.");
    }

    #[test]
    fn a_stray_cleaning_outside_transcribing_changes_nothing() {
        let mut d = Dictation::new();
        let before = d.epoch();
        d.event(Dictate::Cleaning);
        assert_eq!(d.epoch(), before);
        assert_eq!(d.phase(), &Phase::Idle);
    }

    #[test]
    fn a_full_buffer_ends_the_hold_without_a_second_stop() {
        let t = Instant::now();
        let mut d = Dictation::new();
        d.key(true, t);
        d.event(Dictate::Transcribing);
        assert_eq!(d.key(false, at(31_000, t)), None);
    }

    #[test]
    fn a_result_is_pasted_and_empty_speech_is_not() {
        let mut d = Dictation::new();
        d.key(true, Instant::now());
        assert_eq!(
            d.event(Dictate::Dictated {
                text: " Hello. ".into()
            }),
            Some(Action::Paste("Hello.".into()))
        );
        assert_eq!(d.linger(), None, "stays up until the paste reports");
        d.pasted(Ok(()));
        assert!(d.linger().is_some());

        d.key(true, Instant::now());
        assert_eq!(d.event(Dictate::Dictated { text: " ".into() }), None);
        assert_eq!(d.phase(), &Phase::Nothing);
    }

    #[test]
    fn a_failed_paste_says_the_text_is_on_the_clipboard() {
        let mut d = Dictation::new();
        d.key(true, Instant::now());
        d.event(Dictate::Dictated { text: "Hi".into() });
        d.pasted(Err("the desktop refused".into()));
        let pill = d.pill().unwrap();
        assert_eq!(pill.tone, Tone::Warn);
        assert!(pill.text.contains("clipboard"), "{}", pill.text);
    }

    #[test]
    fn a_stale_dismiss_does_not_hide_a_newer_dictation() {
        let mut d = Dictation::new();
        d.key(true, Instant::now());
        d.event(Dictate::Dictated {
            text: String::new(),
        });
        let stale = d.epoch();
        d.key(false, Instant::now());
        d.key(true, Instant::now());
        d.dismiss(stale);
        assert!(matches!(d.phase(), Phase::Listening { .. }));
        let current = d.epoch();
        d.dismiss(current);
        assert_eq!(d.phase(), &Phase::Idle);
    }

    #[test]
    fn the_child_dying_mid_dictation_is_a_failure_but_not_when_idle() {
        let mut d = Dictation::new();
        d.child_exited("gone".into());
        assert_eq!(d.phase(), &Phase::Idle);
        d.key(true, Instant::now());
        d.child_exited("gone".into());
        assert_eq!(d.phase(), &Phase::Failed("gone".into()));
    }

    #[test]
    fn the_pill_shows_the_level_only_while_listening_live() {
        let mut d = Dictation::new();
        assert_eq!(d.pill(), None);
        d.key(true, Instant::now());
        assert_eq!(d.pill().unwrap().level, None);
        d.event(Dictate::Listening);
        d.event(Dictate::Level { rms: 0.05 });
        assert_eq!(d.pill().unwrap().level, Some(0.05));
    }

    #[test]
    fn partials_show_the_newest_text_and_keep_the_meter_until_release() {
        let t = Instant::now();
        let mut d = Dictation::new();
        d.key(true, t);
        d.event(Dictate::Listening);
        assert_eq!(d.pill().unwrap().text, "");
        d.event(Dictate::Partial {
            text: "hello there.".into(),
        });
        d.event(Dictate::Partial {
            text: "hello there. general kenobi.".into(),
        });
        d.event(Dictate::Level { rms: 0.04 });
        let pill = d.pill().unwrap();
        assert_eq!(pill.text, "hello there. general kenobi.");
        assert_eq!(pill.level, Some(0.04));
        d.event(Dictate::Level { rms: 0.09 });
        assert_eq!(d.pill().unwrap().level, Some(0.09));
        assert_eq!(d.key(false, at(2000, t)), Some(Action::Stop));
        assert_eq!(d.phase(), &Phase::Transcribing);
        assert_eq!(d.pill().unwrap().level, None);
        d.event(Dictate::Partial {
            text: "late".into(),
        });
        assert_eq!(d.phase(), &Phase::Transcribing);
    }

    #[test]
    fn partials_survive_latching_hands_free_and_reset_on_the_next_dictation() {
        let t = Instant::now();
        let mut d = Dictation::new();
        d.key(true, t);
        d.event(Dictate::Listening);
        d.event(Dictate::Partial {
            text: "one two".into(),
        });
        d.key(false, at(100, t));
        let pill = d.pill().unwrap();
        assert_eq!(pill.text, "one two");
        d.event(Dictate::Cancelled);
        d.key(true, at(5000, t));
        d.event(Dictate::Listening);
        assert_eq!(d.pill().unwrap().text, "");
    }

    #[test]
    fn a_long_partial_shows_its_newest_words() {
        let mut d = Dictation::new();
        d.key(true, Instant::now());
        d.event(Dictate::Listening);
        let text = format!("{} last words", "word ".repeat(40));
        d.event(Dictate::Partial { text });
        let shown = d.pill().unwrap().text;
        assert_eq!(shown.chars().count(), PREVIEW_CHARS);
        assert!(shown.starts_with('…') && shown.ends_with("last words"));
    }

    #[test]
    fn only_the_words_are_drawn_as_words_and_every_status_is_marked() {
        let t = Instant::now();
        let mut d = Dictation::new();
        d.key(true, t);
        assert!(d.pill().unwrap().status, "starting the microphone");
        d.event(Dictate::Listening);
        assert!(
            !d.pill().unwrap().status,
            "the empty pill is just the meter"
        );
        d.event(Dictate::Partial {
            text: "hello".into(),
        });
        assert!(!d.pill().unwrap().status);
        d.key(false, at(2000, t));
        assert_eq!(d.phase(), &Phase::Transcribing);
        assert!(d.pill().unwrap().status, "transcribing is not the words");
    }

    #[test]
    fn a_long_result_previews_its_end() {
        let text = "word ".repeat(40);
        let shown = preview(text.trim());
        assert_eq!(shown.chars().count(), PREVIEW_CHARS);
        assert!(shown.starts_with('…'));
    }

    #[test]
    fn recent_keeps_five_newest_first() {
        let mut recent = Recent::default();
        for n in 0..7 {
            recent.push(&format!("text {n}"));
        }
        let texts: Vec<&str> = recent.items().map(|(_, text)| text).collect();
        assert_eq!(texts, ["text 6", "text 5", "text 4", "text 3", "text 2"]);
    }

    #[test]
    fn recent_skips_empty_text_and_trims() {
        let mut recent = Recent::default();
        recent.push("");
        recent.push("  \n ");
        assert!(recent.is_empty());
        recent.push("  hello \n");
        assert_eq!(recent.items().next().map(|(_, text)| text), Some("hello"));
    }

    #[test]
    fn recent_clear_empties_it_and_ids_are_not_reused() {
        let mut recent = Recent::default();
        recent.push("one");
        let old = recent.items().next().map(|(id, _)| id).unwrap();
        recent.clear();
        assert!(recent.is_empty());
        recent.push("two");
        assert_eq!(recent.get(old), None);
        assert_eq!(recent.items().count(), 1);
    }

    #[test]
    fn recent_looks_an_entry_up_by_id_even_after_newer_ones_arrive() {
        let mut recent = Recent::default();
        recent.push("first");
        let id = recent.items().next().map(|(id, _)| id).unwrap();
        recent.push("second");
        assert_eq!(recent.get(id), Some("first"));
    }

    #[test]
    fn debug_output_never_carries_the_text() {
        let mut d = Dictation::new();
        let action = d.event(Dictate::Dictated {
            text: "hunter2 secret words".into(),
        });
        let mut recent = Recent::default();
        recent.push("hunter2 secret words");
        let shown = format!("{:?} {:?} {:?}", action, d.phase(), recent);
        assert!(!shown.contains("hunter2"), "{shown}");
    }
}
