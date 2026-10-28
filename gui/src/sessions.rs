use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

use serde::Deserialize;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Part {
    Transcript,
    Summary,
}

impl Part {
    pub fn as_str(self) -> &'static str {
        match self {
            Part::Transcript => "transcript",
            Part::Summary => "summary",
        }
    }
}

// What `--open` names: `<bundle>`, `<bundle>#summary`, `<bundle>#transcript@MM:SS`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Target {
    pub path: PathBuf,
    pub part: Part,
    pub at_s: Option<u64>,
}

pub fn parse_stamp(text: &str) -> Option<u64> {
    let (minutes, seconds) = text.split_once(':')?;
    if minutes.len() < 2 || seconds.len() != 2 {
        return None;
    }
    let minutes: u64 = minutes.parse().ok()?;
    let seconds: u64 = seconds.parse().ok()?;
    (seconds < 60).then_some(minutes * 60 + seconds)
}

pub fn stamp(seconds: u64) -> String {
    format!("{:02}:{:02}", seconds / 60, seconds % 60)
}

impl Target {
    pub fn parse(raw: &str) -> Target {
        // Split at the last '#' only when what follows is a fragment, so a '#' in a path stays.
        if let Some((path, fragment)) = raw.rsplit_once('#') {
            let (part, at) = match fragment.split_once('@') {
                Some((part, at)) => (part, Some(at)),
                None => (fragment, None),
            };
            let at_s = at.map(parse_stamp);
            let part = match part {
                "transcript" => Some(Part::Transcript),
                "summary" if at.is_none() => Some(Part::Summary),
                _ => None,
            };
            if let Some(part) = part
                && at_s != Some(None)
            {
                return Target {
                    path: PathBuf::from(path),
                    part,
                    at_s: at_s.flatten(),
                };
            }
        }
        Target {
            path: PathBuf::from(raw),
            part: Part::Transcript,
            at_s: None,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Default, Deserialize)]
#[serde(default)]
pub struct Entry {
    pub path: PathBuf,
    pub name: String,
    pub started: Option<String>,
    pub duration_s: Option<u64>,
    pub source: Option<String>,
    pub summary: bool,
    pub title: Option<String>,
    pub declined: bool,
    pub failed: bool,
    pub verified: bool,
    pub error: Option<String>,
}

impl Entry {
    pub fn when(&self) -> String {
        match &self.started {
            // "2026-10-27T14:05:12-04:00" reads as "2026-10-27 14:05".
            Some(started) if started.len() >= 16 => started[..16].replace('T', " "),
            _ => self.name.trim_end_matches(".vwsession").to_owned(),
        }
    }

    pub fn length(&self) -> Option<String> {
        let seconds = self.duration_s?;
        Some(if seconds >= 3600 {
            format!("{} h {:02} min", seconds / 3600, (seconds % 3600) / 60)
        } else if seconds >= 60 {
            format!("{} min", seconds / 60)
        } else {
            format!("{seconds} s")
        })
    }

    pub fn status(&self) -> &'static str {
        match self {
            Entry { error: Some(_), .. } => "Unreadable",
            Entry { summary: true, .. } => "Summarised",
            Entry { failed: true, .. } => "Summary failed",
            Entry { declined: true, .. } => "No summary",
            _ => "Waiting for a summary",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Default, Deserialize)]
#[serde(default)]
pub struct Listing {
    pub directory: PathBuf,
    pub auto: bool,
    pub model_ready: bool,
    pub backfill: u64,
    pub sessions: Vec<Entry>,
}

pub fn parse_listing(text: &str) -> Result<Listing, String> {
    serde_json::from_str(text.trim()).map_err(|_| "unreadable session list".to_owned())
}

// A line of a summary, with the stamps in it split out so each can be a link.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Piece {
    Text(String),
    Stamp(u64),
}

pub fn pieces(line: &str) -> Vec<Piece> {
    let mut out = Vec::new();
    let mut rest = line;
    while let Some(open) = rest.find('[') {
        let Some(close) = rest[open..].find(']').map(|i| open + i) else {
            break;
        };
        match parse_stamp(&rest[open + 1..close]) {
            Some(seconds) => {
                if open > 0 {
                    out.push(Piece::Text(rest[..open].to_owned()));
                }
                out.push(Piece::Stamp(seconds));
            }
            None => out.push(Piece::Text(rest[..=close].to_owned())),
        }
        rest = &rest[close + 1..];
    }
    if !rest.is_empty() {
        out.push(Piece::Text(rest.to_owned()));
    }
    out
}

// Paragraphs of a transcript with the second each starts at; the header has none.
pub fn paragraphs(transcript: &str) -> Vec<(Option<u64>, String)> {
    transcript
        .split("\n\n")
        .map(str::trim)
        .filter(|block| !block.is_empty())
        .map(|block| {
            let at = block
                .strip_prefix('[')
                .and_then(|rest| rest.split_once("] "))
                .and_then(|(stamp, _)| parse_stamp(stamp));
            (at, block.to_owned())
        })
        .collect()
}

// The paragraph to scroll to: the last one starting at or before the stamp.
pub fn paragraph_at(paragraphs: &[(Option<u64>, String)], at_s: u64) -> Option<usize> {
    paragraphs
        .iter()
        .enumerate()
        .filter(|(_, (start, _))| start.is_some_and(|start| start <= at_s))
        .map(|(index, _)| index)
        .next_back()
}

fn command() -> PathBuf {
    std::env::current_exe()
        .ok()
        .and_then(|exe| exe.parent().map(|dir| dir.join("vinowhisper-session")))
        .filter(|path| path.is_file())
        .unwrap_or_else(|| PathBuf::from("vinowhisper-session"))
}

fn run(args: &[&std::ffi::OsStr]) -> Result<String, String> {
    let output = Command::new(command())
        .args(args)
        .stdin(Stdio::null())
        .output()
        .map_err(|err| format!("could not run vinowhisper-session: {err}"))?;
    if output.status.success() {
        return Ok(String::from_utf8_lossy(&output.stdout).into_owned());
    }
    let stderr = String::from_utf8_lossy(&output.stderr);
    let reason = stderr
        .lines()
        .rev()
        .find(|line| !line.trim().is_empty())
        .unwrap_or("vinowhisper-session failed")
        .trim()
        .trim_start_matches("[vinowhisper] ");
    Err(reason.to_owned())
}

pub fn list() -> Result<Listing, String> {
    parse_listing(&run(&["list".as_ref(), "--json".as_ref()])?)
}

pub fn show(path: &Path, part: Part) -> Result<String, String> {
    run(&[
        "show".as_ref(),
        path.as_os_str(),
        "--part".as_ref(),
        part.as_str().as_ref(),
    ])
}

pub fn act(action: &str, path: Option<&Path>) -> Result<(), String> {
    let mut args: Vec<&std::ffi::OsStr> = vec![action.as_ref()];
    if let Some(path) = path {
        args.push(path.as_os_str());
    }
    run(&args).map(|_| ())
}

pub fn show_in_folder(path: &Path) {
    // The FileManager1 interface selects the file; xdg-open on the folder is the fallback.
    let uri = format!("file://{}", path.display());
    let selected = Command::new("dbus-send")
        .args([
            "--session",
            "--dest=org.freedesktop.FileManager1",
            "--type=method_call",
            "/org/freedesktop/FileManager1",
            "org.freedesktop.FileManager1.ShowItems",
        ])
        .arg(format!("array:string:{uri}"))
        .arg("string:")
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .is_ok_and(|status| status.success());
    if !selected && let Some(folder) = path.parent() {
        let _ = Command::new("xdg-open")
            .arg(folder)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .map(|mut child| std::thread::spawn(move || child.wait()));
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn open_targets_name_a_part_and_a_stamp() {
        assert_eq!(
            Target::parse("/t/a.vwsession#transcript@61:05"),
            Target {
                path: "/t/a.vwsession".into(),
                part: Part::Transcript,
                at_s: Some(3665)
            }
        );
        assert_eq!(Target::parse("/t/a.vwsession#summary").part, Part::Summary);
        assert_eq!(
            Target::parse("/t/a.vwsession#transcript"),
            Target {
                path: "/t/a.vwsession".into(),
                part: Part::Transcript,
                at_s: None
            }
        );
    }

    #[test]
    fn a_hash_that_is_not_a_fragment_stays_in_the_path() {
        let target = Target::parse("/home/me/take #2.vwsession");
        assert_eq!(target.path, PathBuf::from("/home/me/take #2.vwsession"));
        let target = Target::parse("/t/a.vwsession#summary@00:10");
        assert_eq!(target.path, PathBuf::from("/t/a.vwsession#summary@00:10"));
        let target = Target::parse("/t/a.vwsession#transcript@1:01:05");
        assert_eq!(
            target.path,
            PathBuf::from("/t/a.vwsession#transcript@1:01:05")
        );
    }

    #[test]
    fn stamps_keep_counting_minutes_past_an_hour() {
        assert_eq!(parse_stamp("61:05"), Some(3665));
        assert_eq!(parse_stamp("1:05"), None);
        assert_eq!(parse_stamp("00:60"), None);
        assert_eq!(stamp(3665), "61:05");
    }

    #[test]
    fn a_summary_line_splits_into_text_and_stamp_links() {
        assert_eq!(
            pieces("- Captions run locally [03:10] and [aside]"),
            [
                Piece::Text("- Captions run locally ".into()),
                Piece::Stamp(190),
                Piece::Text(" and [aside]".into()),
            ]
        );
    }

    #[test]
    fn opening_at_a_stamp_lands_on_the_paragraph_holding_it() {
        let text = "vinoWhisper transcript, x\n\n[00:00] One.\n\n[01:10] Two.\n\n[61:05] Three.\n";
        let found = paragraphs(text);
        assert_eq!(found.len(), 4);
        assert_eq!(found[0].0, None);
        assert_eq!(paragraph_at(&found, 100), Some(2));
        assert_eq!(paragraph_at(&found, 3665), Some(3));
        assert_eq!(paragraph_at(&found, 0), Some(1));
    }

    #[test]
    fn the_listing_reads_what_vinowhisper_session_prints() {
        let listing = parse_listing(
            r#"{"directory": "/t", "auto": true, "model_ready": false, "backfill": 2,
               "sessions": [{"path": "/t/a.vwsession", "name": "a.vwsession",
                             "started": "2026-10-27T14:05:12-04:00", "duration_s": 3700,
                             "source": "system audio", "language": "en", "task": "transcribe",
                             "summary": true, "title": "Talk", "summary_made_by": "ai-generated-summary",
                             "declined": false, "failed": false, "verified": true}]}"#,
        )
        .unwrap();
        assert_eq!(listing.backfill, 2);
        let entry = &listing.sessions[0];
        assert_eq!(entry.when(), "2026-10-27 14:05");
        assert_eq!(entry.length().unwrap(), "1 h 01 min");
        assert_eq!(entry.status(), "Summarised");
        assert!(parse_listing("Traceback").is_err());
    }
}
