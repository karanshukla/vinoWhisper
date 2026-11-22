use serde::Deserialize;

#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(tag = "event")]
pub enum Event {
    Ready {
        device: String,
        #[serde(default)]
        degraded: bool,
        #[serde(default)]
        warnings: Vec<String>,
        #[serde(default)]
        language: String,
        #[serde(default)]
        task: String,
    },
    Cycle {
        #[serde(default)]
        confirmed: Vec<String>,
        #[serde(default)]
        pending: Vec<String>,
        total_s: f64,
        #[serde(default)]
        language: String,
        // "you" or "them" when both sources are captioned, empty for one.
        #[serde(default)]
        lane: String,
    },
    Silence {
        elapsed_s: f64,
        #[serde(default)]
        sink_muted: Option<bool>,
        #[serde(default)]
        lane: String,
    },
    Flushed {
        lane: String,
        #[serde(default)]
        words: Vec<String>,
    },
    Stopped {
        #[serde(default)]
        flushed: Vec<String>,
    },
    // Two-lane sessions: how the mic lane is echo-cancelled, and why.
    EchoStatus {
        mode: String,
        active: bool,
        reason: String,
    },
    Hint {
        #[serde(default)]
        kind: String,
        message: String,
    },
    Error {
        message: String,
    },
}

#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(tag = "event")]
pub enum Dictate {
    Listening,
    Level {
        rms: f32,
    },
    Partial {
        text: String,
    },
    Transcribing,
    Cleaning,
    Ready {
        device: String,
        #[serde(default)]
        degraded: bool,
    },
    Dictated {
        text: String,
    },
    Cancelled,
    Error {
        message: String,
    },
    // Sent after `Dictated`: it did not stop the typing.
    Notice {
        message: String,
    },
}

// A newer Python side may add records (pinned by `tests::unknown_records_and_stray_output_are_skipped`).
pub fn parse<T: for<'de> Deserialize<'de>>(line: &str) -> Option<T> {
    serde_json::from_str(line.trim()).ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    const READY: &str = r#"{"event": "Ready", "device": "NPU", "device_full": "Intel(R) AI Boost", "degraded": false, "warnings": [], "server_version": "0.3.1"}"#;
    const DEGRADED: &str = r#"{"event": "Ready", "device": "CPU", "device_full": "Intel(R) Core(TM) Ultra 5", "degraded": true, "warnings": ["No NPU found; running on CPU, expect several times the lag."], "server_version": "0.3.1"}"#;
    const CYCLE: &str = r#"{"event": "Cycle", "index": 3, "captured_s": 14.2, "window_s": 12.0, "hop_s": 1.3, "rms": 0.0312, "gain": 1.6, "first_piece_s": 0.204, "total_s": 1.19, "transcript": "I don’t think so — really.", "confirmed": ["I", "don’t"], "pending": ["think", "so"]}"#;
    const SILENCE: &str =
        r#"{"event": "Silence", "elapsed_s": 46.0, "rms": 0.0, "sink_muted": true}"#;
    const STOPPED: &str = r#"{"event": "Stopped", "flushed": ["so"]}"#;
    const ERROR: &str = r#"{"event": "Error", "message": "Capture failed: pw-record exited with status 1 (see its output above)"}"#;

    #[test]
    fn a_ready_record_names_the_device() {
        assert_eq!(
            parse(READY),
            Some(Event::Ready {
                device: "NPU".into(),
                degraded: false,
                warnings: vec![],
                language: String::new(),
                task: String::new(),
            })
        );
    }

    #[test]
    fn a_degraded_device_keeps_its_warnings() {
        let Some(Event::Ready {
            degraded, warnings, ..
        }) = parse(DEGRADED)
        else {
            panic!("not a Ready record");
        };
        assert!(degraded);
        assert_eq!(warnings.len(), 1);
    }

    #[test]
    fn a_cycle_decodes_escaped_unicode() {
        assert_eq!(
            parse(CYCLE),
            Some(Event::Cycle {
                confirmed: vec!["I".into(), "don’t".into()],
                pending: vec!["think".into(), "so".into()],
                total_s: 1.19,
                language: String::new(),
                lane: String::new(),
            })
        );
    }

    #[test]
    fn silence_stopped_and_error_records_parse() {
        assert_eq!(
            parse(SILENCE),
            Some(Event::Silence {
                elapsed_s: 46.0,
                sink_muted: Some(true),
                lane: String::new(),
            })
        );
        assert_eq!(
            parse(STOPPED),
            Some(Event::Stopped {
                flushed: vec!["so".into()],
            })
        );
        assert!(
            matches!(parse(ERROR), Some(Event::Error { message }) if message.contains("pw-record"))
        );
    }

    #[test]
    fn echo_records_parse() {
        assert_eq!(
            parse(
                r#"{"event": "EchoStatus", "mode": "auto", "active": true, "reason": "on: speakers"}"#
            ),
            Some(Event::EchoStatus {
                mode: "auto".into(),
                active: true,
                reason: "on: speakers".into(),
            })
        );
        assert!(matches!(
            parse(r#"{"event": "Hint", "kind": "headphones", "message": "Use headphones"}"#),
            Some(Event::Hint { .. })
        ));
    }

    #[test]
    fn unknown_records_and_stray_output_are_skipped() {
        assert_eq!(parse::<Event>(r#"{"event": "Heartbeat", "at": 1}"#), None);
        assert_eq!(
            parse::<Event>("[vinowhisper] waiting for the transcription server"),
            None
        );
        assert_eq!(parse::<Event>(""), None);
        assert_eq!(parse::<Dictate>(CYCLE), None);
    }

    #[test]
    fn dictation_records_parse() {
        let lines = [
            r#"{"event": "Listening", "limit_s": 29.5}"#,
            r#"{"event": "Level", "rms": 0.031}"#,
            r#"{"event": "Partial", "text": "It’s"}"#,
            r#"{"event": "Transcribing", "audio_s": 2.4}"#,
            r#"{"event": "Ready", "device": "NPU", "degraded": false, "warnings": []}"#,
            r#"{"event": "Dictated", "text": "It’s done.", "audio_s": 2.4, "total_s": 0.46, "rms": 0.02}"#,
            r#"{"event": "Cancelled"}"#,
            r#"{"event": "Error", "message": "Microphone capture failed"}"#,
            r#"{"event": "Notice", "message": "LLM cleanup paused for 10 min (timed out)"}"#,
        ];
        let parsed: Vec<Dictate> = lines.iter().filter_map(|line| parse(line)).collect();
        assert_eq!(parsed.len(), lines.len());
        assert_eq!(
            parsed[2],
            Dictate::Partial {
                text: "It’s".into()
            }
        );
        assert_eq!(
            parsed[5],
            Dictate::Dictated {
                text: "It’s done.".into()
            }
        );
    }
}
