use std::time::{Duration, Instant};

use crate::captions::Tone;
use crate::protocol::Dictate;

pub const TAP: Duration = Duration::from_millis(350);

const PREVIEW_CHARS: usize = 64;

#[derive(Debug, Clone, PartialEq)]
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

#[derive(Debug, Clone, PartialEq)]
pub enum Action {
    Start,
    HandsFree,
    Stop,
    Paste(String),
}

#[derive(Debug, Clone, PartialEq)]
pub struct Pill {
    pub tone: Tone,
    pub text: String,
    pub level: Option<f32>,
    pub detail: Option<String>,
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
        }
    }

    pub fn phase(&self) -> &Phase {
        &self.phase
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

    fn set(&mut self, phase: Phase) {
        if phase != self.phase {
            self.phase = phase;
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
            self.set(Phase::Idle);
        }
    }

    pub fn linger(&self) -> Option<Duration> {
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
        let (tone, text, level, detail) = match &self.phase {
            Phase::Idle => return None,
            Phase::Listening { live: false, .. } => {
                (Tone::Dim, "Starting the microphone…".to_owned(), None, None)
            }
            Phase::Listening {
                hands_free, text, ..
            } => (
                Tone::Good,
                if *hands_free {
                    "Listening, press again to type"
                } else {
                    "Listening, release to type"
                }
                .to_owned(),
                Some(self.level),
                (!text.is_empty()).then(|| preview(text)),
            ),
            Phase::Transcribing => (Tone::Warn, self.transcribing_text(), None, None),
            Phase::Typed {
                text,
                pasted: None | Some(Ok(())),
            } => (Tone::Good, preview(text), None, None),
            Phase::Typed {
                pasted: Some(Err(why)),
                ..
            } => (
                Tone::Warn,
                format!("On the clipboard, not typed: {why}"),
                None,
                None,
            ),
            Phase::Nothing => (Tone::Dim, "Heard nothing".to_owned(), None, None),
            Phase::Failed(message) => (Tone::Bad, message.clone(), None, None),
        };
        match &self.notice {
            Some((_, message)) => Some(Pill {
                tone: Tone::Warn,
                text: message.clone(),
                level,
                detail: None,
            }),
            None => Some(Pill {
                tone,
                text,
                level,
                detail,
            }),
        }
    }

    fn transcribing_text(&self) -> String {
        match &self.device {
            Some((device, true)) => format!("Transcribing on {device}…"),
            _ => "Transcribing…".to_owned(),
        }
    }
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
        assert_eq!(d.pill().unwrap().text, "Listening, release to type");
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
        assert_eq!(d.pill().unwrap().detail, None);
        d.event(Dictate::Partial {
            text: "hello there.".into(),
        });
        d.event(Dictate::Partial {
            text: "hello there. general kenobi.".into(),
        });
        d.event(Dictate::Level { rms: 0.04 });
        let pill = d.pill().unwrap();
        assert_eq!(pill.text, "Listening, release to type");
        assert_eq!(pill.detail.as_deref(), Some("hello there. general kenobi."));
        assert_eq!(pill.level, Some(0.04));
        d.event(Dictate::Level { rms: 0.09 });
        assert_eq!(d.pill().unwrap().level, Some(0.09));
        assert_eq!(d.key(false, at(2000, t)), Some(Action::Stop));
        assert_eq!(d.phase(), &Phase::Transcribing);
        assert_eq!(d.pill().unwrap().detail, None);
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
        assert_eq!(pill.text, "Listening, press again to type");
        assert_eq!(pill.detail.as_deref(), Some("one two"));
        d.event(Dictate::Cancelled);
        d.key(true, at(5000, t));
        d.event(Dictate::Listening);
        assert_eq!(d.pill().unwrap().detail, None);
    }

    #[test]
    fn a_long_partial_shows_its_newest_words() {
        let mut d = Dictation::new();
        d.key(true, Instant::now());
        d.event(Dictate::Listening);
        let text = format!("{} last words", "word ".repeat(40));
        d.event(Dictate::Partial { text });
        let detail = d.pill().unwrap().detail.unwrap();
        assert_eq!(detail.chars().count(), PREVIEW_CHARS);
        assert!(detail.starts_with('…') && detail.ends_with("last words"));
    }

    #[test]
    fn a_long_result_previews_its_end() {
        let text = "word ".repeat(40);
        let shown = preview(text.trim());
        assert_eq!(shown.chars().count(), PREVIEW_CHARS);
        assert!(shown.starts_with('…'));
    }
}
