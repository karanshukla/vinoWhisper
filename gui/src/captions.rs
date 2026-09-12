//! What the overlay shows, as plain data: no Wayland, no fonts, no threads.
//!
//! Follows the Rich status bar's rules (vinowhisper/ui.py) wherever they
//! apply to a two-line box: confirmed words at full brightness, pending words
//! dimmed after them, and a paragraph break that a real pause *schedules* but
//! only the next word *spends*, so a break never opens onto nothing.
//!
//! Pending words matter more here than in the terminal. The commit policy
//! needs two cycles to agree before a word is confirmed, and in a box that
//! only holds two lines, showing nothing until then reads as a frozen caption
//! rather than as one still being decided.

use std::collections::VecDeque;
use std::time::Instant;

use crate::protocol::Event;

/// Same as `ui._PARAGRAPH_SILENCE_S`: a pause this long is a boundary, not a
/// breath.
const PARAGRAPH_SILENCE_S: f64 = 2.5;

/// Two lines on screen need far fewer than this. The rest is headroom for
/// the small text size, where two lines hold the most words.
const KEEP_WORDS: usize = 160;

/// Cycles averaged for the lag estimate.
const LAG_HISTORY: usize = 8;

/// A second or two of quiet between sentences is ordinary speech and not
/// worth turning the status dot red over.
const NO_SIGNAL_AFTER_S: f64 = 3.0;

/// The status line is one line of small text; a device warning longer than
/// this is in the server journal and in vinowhisper-doctor in full.
const STATUS_CHARS: usize = 72;

#[derive(Debug, Clone, PartialEq)]
pub enum Phase {
    /// The process is up and waiting on the server, possibly through a cold
    /// NPU model load.
    Starting,
    Live,
    Stopped,
    Failed(String),
}

/// A colour role, resolved to an actual colour by the painter.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Tone {
    Caption,
    Pending,
    Dim,
    Good,
    Warn,
    Bad,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Span {
    pub text: String,
    pub tone: Tone,
}

impl Span {
    fn new(text: impl Into<String>, tone: Tone) -> Self {
        Span {
            text: text.into(),
            tone,
        }
    }
}

#[derive(Debug, Clone, PartialEq)]
enum Token {
    Word(String),
    Break,
}

#[derive(Debug)]
pub struct Captions {
    tokens: VecDeque<Token>,
    pending: Vec<String>,
    break_pending: bool,
    phase: Phase,
    started_at: Instant,
    device: Option<String>,
    degraded: bool,
    warning: Option<String>,
    cycles: VecDeque<f64>,
    silent_for: Option<f64>,
}

impl Default for Captions {
    fn default() -> Self {
        Self::new()
    }
}

impl Captions {
    pub fn new() -> Self {
        Captions {
            tokens: VecDeque::new(),
            pending: Vec::new(),
            break_pending: false,
            phase: Phase::Starting,
            started_at: Instant::now(),
            device: None,
            degraded: false,
            warning: None,
            cycles: VecDeque::new(),
            silent_for: None,
        }
    }

    /// A new session. Whatever the last one said is not this one's to show.
    pub fn reset(&mut self) {
        *self = Self::new();
    }

    pub fn phase(&self) -> &Phase {
        &self.phase
    }

    pub fn device(&self) -> Option<&str> {
        self.device.as_deref()
    }

    pub fn degraded(&self) -> bool {
        self.degraded
    }

    pub fn apply(&mut self, event: Event) {
        match event {
            Event::Ready {
                device,
                degraded,
                warnings,
            } => {
                self.device = Some(device);
                self.degraded = degraded;
                self.warning = warnings.into_iter().next();
                self.phase = Phase::Live;
            }
            Event::Cycle {
                confirmed,
                pending,
                total_s,
            } => {
                self.phase = Phase::Live;
                self.silent_for = None;
                if self.cycles.len() == LAG_HISTORY {
                    self.cycles.pop_front();
                }
                self.cycles.push_back(total_s);
                self.pending = pending;
                self.add_words(confirmed);
            }
            Event::Silence { elapsed_s, .. } => {
                self.silent_for = Some(elapsed_s);
                if elapsed_s >= PARAGRAPH_SILENCE_S {
                    // Scheduled, not applied: Silence repeats every cycle it
                    // stays quiet. add_words spends it on the next real word.
                    self.break_pending = true;
                }
            }
            Event::Stopped { flushed } => {
                self.add_words(flushed);
                self.pending.clear();
                self.ended();
            }
            Event::Error { message } => self.fail(message),
        }
    }

    pub fn fail(&mut self, message: impl Into<String>) {
        self.pending.clear();
        self.phase = Phase::Failed(message.into());
    }

    /// The process is gone. Anything already on screen stays; a failure
    /// already recorded is the better explanation, so it is kept.
    pub fn ended(&mut self) {
        if !matches!(self.phase, Phase::Failed(_)) {
            self.phase = Phase::Stopped;
        }
    }

    /// The same estimate the terminal shows: the commit policy needs two
    /// cycles to agree and the hop is whatever the last cycle took, so this
    /// is the floor on how far behind the audio a caption lands.
    pub fn lag_s(&self) -> Option<f64> {
        if self.cycles.is_empty() {
            return None;
        }
        let mean = self.cycles.iter().sum::<f64>() / self.cycles.len() as f64;
        Some(2.0 * mean)
    }

    fn add_words(&mut self, words: Vec<String>) {
        for word in words {
            if self.break_pending && matches!(self.tokens.back(), Some(Token::Word(_))) {
                self.tokens.push_back(Token::Break);
            }
            self.break_pending = false;
            self.tokens.push_back(Token::Word(word));
        }
        while self.tokens.len() > KEEP_WORDS {
            self.tokens.pop_front();
        }
        while matches!(self.tokens.front(), Some(Token::Break)) {
            self.tokens.pop_front();
        }
    }

    /// The caption body: confirmed words, then pending ones, or a
    /// placeholder saying why there are neither.
    pub fn caption_spans(&self) -> Vec<Span> {
        let mut text = String::new();
        for token in &self.tokens {
            match token {
                Token::Word(word) => {
                    if !text.is_empty() && !text.ends_with('\n') {
                        text.push(' ');
                    }
                    text.push_str(word);
                }
                Token::Break => text.push('\n'),
            }
        }

        let mut spans = Vec::new();
        let lead = if text.is_empty() || text.ends_with('\n') {
            ""
        } else {
            " "
        };
        if !text.is_empty() {
            spans.push(Span::new(text, Tone::Caption));
        }
        if !self.pending.is_empty() {
            spans.push(Span::new(
                format!("{lead}{}", self.pending.join(" ")),
                Tone::Pending,
            ));
        }
        if spans.is_empty() {
            spans.push(self.placeholder());
        }
        spans
    }

    fn placeholder(&self) -> Span {
        match &self.phase {
            Phase::Starting => Span::new(
                format!(
                    "Starting… {}s. A cold start loads the model onto the NPU, \
                     which takes 10 to 30 seconds.",
                    self.started_at.elapsed().as_secs()
                ),
                Tone::Dim,
            ),
            Phase::Live if self.silent_for.is_some_and(|s| s >= NO_SIGNAL_AFTER_S) => Span::new(
                "Listening. Nothing audible on the capture target yet.",
                Tone::Dim,
            ),
            Phase::Live => Span::new("Listening…", Tone::Dim),
            Phase::Stopped => Span::new("Captions stopped.", Tone::Dim),
            Phase::Failed(message) => Span::new(message.clone(), Tone::Bad),
        }
    }

    /// The status dot's colour, then the line of small text beside it.
    pub fn status(&self) -> (Tone, Vec<Span>) {
        let silent = self.silent_for.filter(|s| *s >= NO_SIGNAL_AFTER_S);
        let (dot, label) = match (&self.phase, silent) {
            (Phase::Starting, _) => (Tone::Warn, "starting".to_owned()),
            (Phase::Live, Some(seconds)) => (Tone::Bad, format!("no signal {seconds:.0}s")),
            (Phase::Live, None) => (Tone::Good, "live".to_owned()),
            (Phase::Stopped, _) => (Tone::Dim, "stopped".to_owned()),
            (Phase::Failed(_), _) => (Tone::Bad, "stopped".to_owned()),
        };

        let mut spans = vec![Span::new(label, Tone::Dim)];
        if let Some(device) = &self.device {
            spans.push(Span::new(" · ", Tone::Dim));
            // A device below the NPU is never silent (see events.Ready):
            // here it is coloured, and the painter also borders the box.
            let tone = if self.degraded { Tone::Warn } else { Tone::Dim };
            spans.push(Span::new(device.clone(), tone));
        }
        if self.phase == Phase::Live
            && let Some(lag) = self.lag_s()
        {
            spans.push(Span::new(format!(" · lag ~{lag:.1}s"), Tone::Dim));
        }
        if self.degraded
            && let Some(warning) = &self.warning
        {
            spans.push(Span::new(
                format!(" · ⚠ {}", truncate(warning, STATUS_CHARS)),
                Tone::Warn,
            ));
        }
        // With words still on screen the body keeps them, so the reason the
        // session ended has to go here instead.
        if let Phase::Failed(message) = &self.phase
            && !self.tokens.is_empty()
        {
            spans.push(Span::new(
                format!(" · {}", truncate(message, STATUS_CHARS)),
                Tone::Bad,
            ));
        }
        (dot, spans)
    }
}

fn truncate(text: &str, max_chars: usize) -> String {
    let first_line = text.lines().next().unwrap_or("");
    if first_line.chars().count() <= max_chars {
        return first_line.to_owned();
    }
    let kept: String = first_line.chars().take(max_chars - 1).collect();
    format!("{}…", kept.trim_end())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn cycle(confirmed: &[&str], pending: &[&str]) -> Event {
        Event::Cycle {
            confirmed: confirmed.iter().map(|w| w.to_string()).collect(),
            pending: pending.iter().map(|w| w.to_string()).collect(),
            total_s: 1.2,
        }
    }

    fn silence(elapsed_s: f64) -> Event {
        Event::Silence {
            elapsed_s,
            sink_muted: None,
        }
    }

    fn body(captions: &Captions) -> Vec<(String, Tone)> {
        captions
            .caption_spans()
            .into_iter()
            .map(|span| (span.text, span.tone))
            .collect()
    }

    #[test]
    fn confirmed_words_come_first_and_pending_ones_follow_dimmed() {
        let mut captions = Captions::new();
        captions.apply(cycle(&["Hello,", "world."], &["And"]));
        assert_eq!(
            body(&captions),
            vec![
                ("Hello, world.".to_owned(), Tone::Caption),
                (" And".to_owned(), Tone::Pending),
            ]
        );
    }

    #[test]
    fn a_pause_breaks_the_paragraph_only_once_a_word_arrives() {
        let mut captions = Captions::new();
        captions.apply(cycle(&["first."], &[]));
        captions.apply(silence(PARAGRAPH_SILENCE_S + 1.0));
        captions.apply(silence(PARAGRAPH_SILENCE_S + 2.0));
        assert_eq!(
            body(&captions)[0].0,
            "first.",
            "no break until a word needs it"
        );

        captions.apply(cycle(&["second."], &[]));
        assert_eq!(body(&captions)[0].0, "first.\nsecond.");
    }

    #[test]
    fn a_short_pause_is_a_breath_not_a_paragraph() {
        let mut captions = Captions::new();
        captions.apply(cycle(&["one"], &[]));
        captions.apply(silence(1.0));
        captions.apply(cycle(&["two"], &[]));
        assert_eq!(body(&captions)[0].0, "one two");
    }

    #[test]
    fn a_session_never_opens_on_a_break() {
        let mut captions = Captions::new();
        captions.apply(silence(10.0));
        captions.apply(cycle(&["hello"], &[]));
        assert_eq!(body(&captions)[0].0, "hello");
    }

    #[test]
    fn old_words_are_dropped_but_the_newest_stay() {
        let mut captions = Captions::new();
        let words: Vec<String> = (0..KEEP_WORDS * 2).map(|i| format!("w{i}")).collect();
        let refs: Vec<&str> = words.iter().map(String::as_str).collect();
        captions.apply(cycle(&refs, &[]));
        let text = &body(&captions)[0].0;
        assert_eq!(text.split(' ').count(), KEEP_WORDS);
        assert!(text.ends_with(&format!("w{}", KEEP_WORDS * 2 - 1)));
    }

    #[test]
    fn stopping_releases_the_flushed_words_and_clears_pending() {
        let mut captions = Captions::new();
        captions.apply(cycle(&["almost"], &["done"]));
        captions.apply(Event::Stopped {
            flushed: vec!["done".into()],
        });
        assert_eq!(
            body(&captions),
            vec![("almost done".to_owned(), Tone::Caption)]
        );
        assert_eq!(captions.phase(), &Phase::Stopped);
    }

    #[test]
    fn an_error_with_nothing_on_screen_takes_the_body() {
        let mut captions = Captions::new();
        captions.apply(Event::Error {
            message: "The transcription server is not reachable.".into(),
        });
        assert_eq!(
            body(&captions),
            vec![(
                "The transcription server is not reachable.".to_owned(),
                Tone::Bad
            )]
        );
    }

    #[test]
    fn an_error_after_words_goes_on_the_status_line_and_the_words_stay() {
        let mut captions = Captions::new();
        captions.apply(cycle(&["kept"], &[]));
        captions.fail("Capture failed: pw-record exited with status 1");
        assert_eq!(body(&captions)[0].0, "kept");
        let (dot, status) = captions.status();
        assert_eq!(dot, Tone::Bad);
        assert!(
            status
                .iter()
                .any(|span| span.tone == Tone::Bad && span.text.contains("pw-record exited"))
        );
    }

    #[test]
    fn the_process_ending_does_not_overwrite_the_reason_it_failed() {
        let mut captions = Captions::new();
        captions.fail("Capture failed");
        captions.ended();
        assert_eq!(captions.phase(), &Phase::Failed("Capture failed".into()));
    }

    #[test]
    fn lag_is_twice_the_mean_cycle() {
        let mut captions = Captions::new();
        assert_eq!(captions.lag_s(), None);
        for total_s in [1.0, 2.0, 3.0] {
            captions.apply(Event::Cycle {
                confirmed: vec![],
                pending: vec![],
                total_s,
            });
        }
        assert_eq!(captions.lag_s(), Some(4.0));
    }

    #[test]
    fn a_degraded_device_is_coloured_and_carries_its_warning() {
        let mut captions = Captions::new();
        captions.apply(Event::Ready {
            device: "CPU".into(),
            degraded: true,
            warnings: vec!["No NPU found; captions will lag far more than on the NPU.".into()],
        });
        let (_, status) = captions.status();
        assert!(
            status
                .iter()
                .any(|s| s.text == "CPU" && s.tone == Tone::Warn)
        );
        assert!(
            status
                .iter()
                .any(|s| s.text.contains("No NPU found") && s.tone == Tone::Warn)
        );
        assert!(captions.degraded());
    }

    #[test]
    fn the_npu_is_not_a_warning() {
        let mut captions = Captions::new();
        captions.apply(Event::Ready {
            device: "NPU".into(),
            degraded: false,
            warnings: vec![],
        });
        let (dot, status) = captions.status();
        assert_eq!(dot, Tone::Good);
        assert!(!status.iter().any(|s| s.tone == Tone::Warn));
    }

    #[test]
    fn sustained_silence_turns_the_dot_red_and_says_so() {
        let mut captions = Captions::new();
        captions.apply(Event::Ready {
            device: "NPU".into(),
            degraded: false,
            warnings: vec![],
        });
        captions.apply(silence(1.0));
        assert_eq!(captions.status().0, Tone::Good, "a breath is not a fault");
        captions.apply(silence(12.0));
        let (dot, status) = captions.status();
        assert_eq!(dot, Tone::Bad);
        assert_eq!(status[0].text, "no signal 12s");
    }

    #[test]
    fn long_warnings_are_cut_to_one_line() {
        assert_eq!(truncate("short", 10), "short");
        assert_eq!(truncate("first line\nsecond", 20), "first line");
        assert_eq!(truncate("abcdefghijk", 5), "abcd…");
    }
}
