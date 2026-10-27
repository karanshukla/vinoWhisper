use std::io::{BufRead, BufReader};
use std::path::PathBuf;
use std::process::{Command, Stdio};

use serde_json::Value;

#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Status {
    pub label: String,
    pub download_bytes: u64,
    pub present: bool,
    pub bytes: u64,
    pub cache_bytes: u64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Line {
    Progress { done: u64, total: u64 },
    Done,
    Deleted,
    Error(String),
    Other,
}

#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct ModelState {
    pub status: Option<Result<Status, String>>,
    pub fetching: Option<(u64, u64)>,
    pub busy: bool,
    pub error: String,
}

fn number(value: &Value, key: &str) -> u64 {
    value.get(key).and_then(Value::as_u64).unwrap_or(0)
}

pub fn parse_status(line: &str) -> Result<Status, String> {
    let value: Value =
        serde_json::from_str(line.trim()).map_err(|_| "unreadable model status".to_owned())?;
    if !value.is_object() {
        return Err("unreadable model status".into());
    }
    Ok(Status {
        label: value
            .get("label")
            .and_then(Value::as_str)
            .unwrap_or("Cleanup model")
            .to_owned(),
        download_bytes: number(&value, "download_bytes"),
        present: value.get("present").and_then(Value::as_bool) == Some(true),
        bytes: number(&value, "bytes"),
        cache_bytes: number(&value, "cache_bytes"),
    })
}

pub fn parse_line(line: &str) -> Line {
    let Ok(value) = serde_json::from_str::<Value>(line.trim()) else {
        return Line::Other;
    };
    match value.get("event").and_then(Value::as_str) {
        Some("progress") => Line::Progress {
            done: number(&value, "done"),
            total: number(&value, "total"),
        },
        Some("done") => Line::Done,
        Some("deleted") => Line::Deleted,
        Some("error") => Line::Error(
            value
                .get("message")
                .and_then(Value::as_str)
                .unwrap_or("unknown error")
                .to_owned(),
        ),
        _ => Line::Other,
    }
}

pub fn size(bytes: u64) -> String {
    const UNITS: [&str; 4] = ["KB", "MB", "GB", "TB"];
    if bytes < 1000 {
        return format!("{bytes} B");
    }
    let mut value = bytes as f64;
    let mut unit = "B";
    for next in UNITS {
        if value < 1000.0 {
            break;
        }
        value /= 1000.0;
        unit = next;
    }
    if value < 10.0 {
        format!("{value:.1} {unit}")
    } else {
        format!("{value:.0} {unit}")
    }
}

fn command() -> PathBuf {
    std::env::current_exe()
        .ok()
        .and_then(|exe| exe.parent().map(|dir| dir.join("vinowhisper-cleanup")))
        .filter(|path| path.is_file())
        .unwrap_or_else(|| PathBuf::from("vinowhisper-cleanup"))
}

fn spawn(action: &str) -> Result<std::process::Child, String> {
    Command::new(command())
        .args(["model", action])
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|err| format!("could not run vinowhisper-cleanup: {err}"))
}

pub fn status() -> Result<Status, String> {
    let output = spawn("status")?
        .wait_with_output()
        .map_err(|err| err.to_string())?;
    let text = String::from_utf8_lossy(&output.stdout);
    parse_status(text.lines().last().unwrap_or(""))
}

pub fn run(action: &str, mut on_line: impl FnMut(Line)) -> Result<(), String> {
    let mut child = spawn(action)?;
    let mut failed = None;
    if let Some(stdout) = child.stdout.take() {
        for line in BufReader::new(stdout).lines().map_while(Result::ok) {
            let parsed = parse_line(&line);
            if let Line::Error(message) = &parsed {
                failed = Some(message.clone());
            }
            on_line(parsed);
        }
    }
    let exit = child.wait().map_err(|err| err.to_string())?;
    match failed {
        Some(message) => Err(message),
        None if !exit.success() => Err(format!("vinowhisper-cleanup exited ({exit})")),
        None => Ok(()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn status_reads_the_fields_the_page_shows() {
        let status = parse_status(
            r#"{"label": "SmolLM3-3B", "repo": "arankays/SmolLM3-3B-int4-cw-ov", "revision": "136eac7", "download_bytes": 1702000000, "present": true, "bytes": 1701999000, "cache_bytes": 2000000000, "dir": "/x"}"#,
        )
        .unwrap();
        assert_eq!(
            status,
            Status {
                label: "SmolLM3-3B".into(),
                download_bytes: 1_702_000_000,
                present: true,
                bytes: 1_701_999_000,
                cache_bytes: 2_000_000_000,
            }
        );
    }

    #[test]
    fn a_missing_field_is_not_downloaded_and_garbage_is_an_error() {
        let status = parse_status(r#"{"label": "SmolLM3-3B"}"#).unwrap();
        assert!(!status.present);
        assert_eq!(status.bytes, 0);
        assert!(parse_status("Traceback (most recent call last):").is_err());
        assert!(parse_status("[1, 2]").is_err());
    }

    #[test]
    fn fetch_lines_are_progress_then_done_or_an_error() {
        assert_eq!(
            parse_line(r#"{"event": "progress", "done": 10, "total": 40}"#),
            Line::Progress {
                done: 10,
                total: 40
            }
        );
        assert_eq!(parse_line(r#"{"event": "done", "bytes": 40}"#), Line::Done);
        assert_eq!(parse_line(r#"{"event": "deleted"}"#), Line::Deleted);
        assert_eq!(
            parse_line(r#"{"event": "error", "message": "sha256 mismatch"}"#),
            Line::Error("sha256 mismatch".into())
        );
        assert_eq!(parse_line("Downloading…"), Line::Other);
        assert_eq!(parse_line(r#"{"event": "later"}"#), Line::Other);
    }

    #[test]
    fn sizes_read_like_the_download_dialogs_do() {
        assert_eq!(size(0), "0 B");
        assert_eq!(size(1_702_000_000), "1.7 GB");
        assert_eq!(size(45_000_000), "45 MB");
        assert_eq!(size(2_500), "2.5 KB");
    }
}
