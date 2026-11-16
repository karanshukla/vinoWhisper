use std::ffi::OsStr;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

use serde::Deserialize;

use crate::forward::beside_us_or_on_path;
pub use vinowhisper_settings::OpenIn;

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

// `<bundle>`, `<bundle>#summary` or `<bundle>#transcript@MM:SS` (pinned by
// `tests::open_targets_name_a_part_and_a_stamp`).
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
        // (pinned by `tests::a_hash_that_is_not_a_fragment_stays_in_the_path`)
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
    /// What was sending the audio, already worded by vinowhisper-session.
    pub playing: Option<String>,
    pub summary: bool,
    pub title: Option<String>,
    pub declined: bool,
    pub failed: bool,
    pub verified: bool,
    pub error: Option<String>,
}

/// The length of an RFC 3339 time cut to the minute, `2026-10-27T14:05`.
const TO_THE_MINUTE: usize = 16;

impl Entry {
    // (pinned by `tests::a_session_is_named_by_its_start_to_the_minute`,
    // `tests::a_session_without_a_full_start_time_is_named_by_its_file`)
    pub fn when(&self) -> String {
        match &self.started {
            Some(started) if started.len() >= TO_THE_MINUTE => {
                started[..TO_THE_MINUTE].replace('T', " ")
            }
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
    /// The session the server is summarising now, and those asked for after it.
    pub current: Option<PathBuf>,
    pub queued: Vec<PathBuf>,
    pub directory: PathBuf,
    pub auto: bool,
    pub model_ready: bool,
    pub backfill: u64,
    pub sessions: Vec<Entry>,
}

impl Listing {
    pub fn working(&self) -> bool {
        self.current.is_some() || !self.queued.is_empty()
    }

    pub fn status(&self, entry: &Entry) -> &'static str {
        if self.current.as_ref() == Some(&entry.path) {
            "Summarising…"
        } else if self.queued.contains(&entry.path) {
            "Queued for a summary"
        } else {
            entry.status()
        }
    }
}

pub fn parse_listing(text: &str) -> Result<Listing, String> {
    serde_json::from_str(text.trim()).map_err(|_| "unreadable session list".to_owned())
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Piece {
    Text(String),
    Stamp(u64),
}

// Older summaries carry the model's own "(0:02)" beside "[00:02]" (pinned by
// `tests::the_models_own_stamps_are_links_and_other_brackets_are_text`).
fn loose_stamp(text: &str) -> Option<u64> {
    let (minutes, seconds) = text.split_once(':')?;
    if minutes.is_empty() {
        return None;
    }
    parse_stamp(&format!("{minutes:0>2}:{seconds}"))
}

pub fn pieces(line: &str) -> Vec<Piece> {
    let mut out = Vec::new();
    let mut rest = line;
    while let Some(open) = rest.find(['[', '(']) {
        let closer = if rest[open..].starts_with('[') {
            ']'
        } else {
            ')'
        };
        let Some(close) = rest[open..].find(closer).map(|i| open + i) else {
            break;
        };
        match loose_stamp(&rest[open + 1..close]) {
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

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SummaryLine {
    Heading(String),
    Bullet(String),
    Footer(String),
    Text(String),
    Blank,
}

// The model strays from "# " and "- " (pinned by
// `tests::summary_lines_are_drawn_as_what_they_mean_not_their_marks`).
pub fn summary_line(line: &str) -> SummaryLine {
    let line = line.trim_end();
    if line.trim().is_empty() {
        return SummaryLine::Blank;
    }
    let hashes = line.len() - line.trim_start_matches('#').len();
    if (1..=6).contains(&hashes) && line[hashes..].starts_with(' ') {
        return SummaryLine::Heading(line[hashes..].trim().trim_end_matches(':').to_owned());
    }
    let trimmed = line.trim_start();
    if let Some(item) = trimmed
        .strip_prefix("- ")
        .or_else(|| trimmed.strip_prefix("* "))
    {
        return SummaryLine::Bullet(item.trim().to_owned());
    }
    if trimmed.len() > 2 && trimmed.starts_with('_') && trimmed.ends_with('_') {
        return SummaryLine::Footer(trimmed.trim_matches('_').to_owned());
    }
    SummaryLine::Text(line.to_owned())
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Paragraph {
    /// `None` for the transcript's header.
    pub start_s: Option<u64>,
    pub text: String,
}

pub fn paragraphs(transcript: &str) -> Vec<Paragraph> {
    transcript
        .split("\n\n")
        .map(str::trim)
        .filter(|block| !block.is_empty())
        .map(|block| {
            let start_s = block
                .strip_prefix('[')
                .and_then(|rest| rest.split_once("] "))
                .and_then(|(stamp, _)| parse_stamp(stamp));
            Paragraph {
                start_s,
                text: block.to_owned(),
            }
        })
        .collect()
}

// The last one starting at or before `at_s` (pinned by
// `tests::opening_at_a_stamp_lands_on_the_paragraph_holding_it`).
pub fn paragraph_at(paragraphs: &[Paragraph], at_s: u64) -> Option<usize> {
    paragraphs
        .iter()
        .enumerate()
        .filter(|(_, p)| p.start_s.is_some_and(|start| start <= at_s))
        .map(|(index, _)| index)
        .next_back()
}

fn command() -> PathBuf {
    beside_us_or_on_path("vinowhisper-session")
}

fn run(args: &[&OsStr]) -> Result<String, String> {
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
    let mut args: Vec<&OsStr> = vec![action.as_ref()];
    if let Some(path) = path {
        args.push(path.as_os_str());
    }
    run(&args).map(|_| ())
}

pub fn show_in_folder(path: &Path) {
    if !select_in_file_manager(path)
        && let Some(folder) = path.parent()
    {
        let _ = Command::new("xdg-open")
            .arg(folder)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .map(|mut child| std::thread::spawn(move || child.wait()));
    }
}

fn select_in_file_manager(path: &Path) -> bool {
    let uri = format!("file://{}", path.display());
    Command::new("dbus-send")
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
        .is_ok_and(|status| status.success())
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Scope {
    Both,
    Summary,
    Transcript,
}

impl Scope {
    pub fn as_str(self) -> &'static str {
        match self {
            Scope::Both => "both",
            Scope::Summary => "summary",
            Scope::Transcript => "transcript",
        }
    }
}

impl From<Part> for Scope {
    fn from(part: Part) -> Scope {
        match part {
            Part::Summary => Scope::Summary,
            Part::Transcript => Scope::Transcript,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Format {
    Markdown,
    Text,
    Odt,
}

impl Format {
    pub fn as_str(self) -> &'static str {
        match self {
            Format::Markdown => "md",
            Format::Text => "txt",
            Format::Odt => "odt",
        }
    }

    pub fn label(self) -> &'static str {
        match self {
            Format::Markdown => "Markdown",
            Format::Text => "Plain text",
            Format::Odt => "OpenDocument",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ExportRequest {
    pub path: PathBuf,
    pub scope: Scope,
    pub format: Format,
}

#[derive(Debug, Clone, PartialEq, Eq, Default, Deserialize)]
#[serde(default)]
pub struct Exported {
    pub path: PathBuf,
    pub parts: Vec<String>,
    pub summary_missing: bool,
    pub state: String,
    #[serde(skip)]
    pub saved_without_dialog: bool,
}

fn export_args<'a>(request: &'a ExportRequest, out: Option<&'a Path>) -> Vec<&'a OsStr> {
    let mut args: Vec<&OsStr> = vec![
        "export".as_ref(),
        request.path.as_os_str(),
        "--part".as_ref(),
        request.scope.as_str().as_ref(),
        "--format".as_ref(),
        request.format.as_str().as_ref(),
    ];
    if let Some(out) = out {
        args.extend(["-o".as_ref(), out.as_os_str()]);
    }
    args
}

pub fn export_path(request: &ExportRequest) -> Result<PathBuf, String> {
    let mut args = export_args(request, None);
    args.push("--print-path".as_ref());
    Ok(PathBuf::from(run(&args)?.trim_end_matches('\n')))
}

pub fn export(
    request: &ExportRequest,
    out: Option<&Path>,
    force: bool,
) -> Result<Exported, String> {
    let mut args = export_args(request, out);
    args.push("--json".as_ref());
    if force {
        args.push("--force".as_ref());
    }
    serde_json::from_str(run(&args)?.trim()).map_err(|_| "unreadable export result".to_owned())
}

#[derive(Debug, PartialEq, Eq)]
pub enum Chosen {
    Path(PathBuf),
    Cancelled,
    NoPortal { reason: String },
}

// (pinned by `tests::a_file_uri_from_the_portal_is_a_local_path`)
pub fn file_uri_path(uri: &str) -> Option<PathBuf> {
    use std::os::unix::ffi::OsStringExt;
    let rest = uri.strip_prefix("file://")?;
    let rest = rest.strip_prefix("localhost").unwrap_or(rest);
    if !rest.starts_with('/') {
        return None;
    }
    let bytes = rest.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%' {
            let hex = std::str::from_utf8(bytes.get(i + 1..i + 3)?).ok()?;
            out.push(u8::from_str_radix(hex, 16).ok()?);
            i += 3;
        } else {
            out.push(bytes[i]);
            i += 1;
        }
    }
    Some(PathBuf::from(std::ffi::OsString::from_vec(out)))
}

pub fn save_dialog(suggested: &Path, format: Format) -> Chosen {
    use ashpd::desktop::file_chooser::{FileFilter, SelectedFiles};
    let name = suggested
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default();
    // The portal only opens a folder that exists.
    let folder = suggested
        .parent()
        .filter(|dir| std::fs::create_dir_all(dir).is_ok());
    let asked = async_io::block_on(async {
        SelectedFiles::save_file()
            .title("Export session")
            .accept_label("Export")
            .modal(true)
            .current_name(name.as_str())
            .current_folder::<&Path>(folder)?
            .filter(FileFilter::new(format.label()).glob(&format!("*.{}", format.as_str())))
            .send()
            .await?
            .response()
    });
    chosen(asked)
}

fn chosen(asked: Result<ashpd::desktop::file_chooser::SelectedFiles, ashpd::Error>) -> Chosen {
    use ashpd::desktop::ResponseError;
    match asked {
        Ok(files) => match files
            .uris()
            .first()
            .and_then(|uri| file_uri_path(uri.as_str()))
        {
            Some(path) => Chosen::Path(path),
            None => Chosen::NoPortal {
                reason: "the file dialog gave no local file".to_owned(),
            },
        },
        Err(ashpd::Error::Response(ResponseError::Cancelled)) => Chosen::Cancelled,
        Err(err) => Chosen::NoPortal {
            reason: err.to_string(),
        },
    }
}

pub fn open_dialog() -> Chosen {
    use ashpd::desktop::file_chooser::{FileFilter, SelectedFiles};
    let asked = async_io::block_on(async {
        SelectedFiles::open_file()
            .title("Import audio or video")
            .accept_label("Import")
            .modal(true)
            .multiple(false)
            .filter(media_filter())
            .filter(FileFilter::new("All files").glob("*"))
            .send()
            .await?
            .response()
    });
    chosen(asked)
}

// Globs, not "audio/*": a portal looks MIME types up by name (pinned by
// `tests::the_file_dialog_filters_by_extension_in_either_case_not_by_wildcard_mime`).
const MEDIA_EXTENSIONS: [&str; 20] = [
    "mp3", "m4a", "m4b", "aac", "wav", "flac", "ogg", "oga", "opus", "wma", "aiff", "amr", "mp4",
    "m4v", "mkv", "webm", "mov", "avi", "mpg", "wmv",
];

fn media_globs() -> Vec<String> {
    MEDIA_EXTENSIONS
        .iter()
        .flat_map(|ext| [format!("*.{ext}"), format!("*.{}", ext.to_uppercase())])
        .collect()
}

fn media_filter() -> ashpd::desktop::file_chooser::FileFilter {
    media_globs().iter().fold(
        ashpd::desktop::file_chooser::FileFilter::new("Audio and video"),
        |filter, glob| filter.glob(glob),
    )
}

#[derive(Debug, Clone, PartialEq)]
pub enum ImportEvent {
    Chosen(String),
    Started {
        pid: u32,
    },
    // The pid is about to be reaped and must not be signalled.
    OutputEnded,
    Progress {
        position_s: f64,
        duration_s: Option<f64>,
    },
    Warning(String),
    Done(PathBuf),
    Failed(String),
}

pub fn parse_import_line(line: &str) -> Option<ImportEvent> {
    let record: serde_json::Value = serde_json::from_str(line.trim()).ok()?;
    match record.get("event")?.as_str()? {
        "Progress" => Some(ImportEvent::Progress {
            position_s: record.get("position_s")?.as_f64()?,
            duration_s: record.get("duration_s").and_then(|d| d.as_f64()),
        }),
        "Done" => Some(ImportEvent::Done(PathBuf::from(
            record.get("path")?.as_str()?,
        ))),
        "Warning" => Some(ImportEvent::Warning(
            record.get("message")?.as_str()?.to_owned(),
        )),
        "Error" => Some(ImportEvent::Failed(
            record.get("message")?.as_str()?.to_owned(),
        )),
        _ => None,
    }
}

pub fn import(file: &Path, on_event: &mut dyn FnMut(ImportEvent)) -> Result<PathBuf, String> {
    use std::io::BufRead;
    use std::os::unix::process::CommandExt;
    let mut command = Command::new(beside_us_or_on_path("vinowhisper-transcribe"));
    command
        // After "--": a file name may start with a dash.
        .args(["--json", "--"])
        .arg(file)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null());
    // SAFETY: prctl only, which is async-signal-safe. An import must not outlive the window,
    // which is the only way to cancel it; the signal follows this thread, which waits on it.
    unsafe {
        command.pre_exec(|| {
            rustix::process::set_parent_process_death_signal(Some(rustix::process::Signal::TERM))
                .map_err(std::io::Error::from)
        });
    }
    let mut child = command.spawn().map_err(|err| {
        format!("could not run vinowhisper-transcribe ({err}); vinowhisper-setup links it")
    })?;
    on_event(ImportEvent::Started { pid: child.id() });
    let mut outcome = None;
    if let Some(stdout) = child.stdout.take() {
        for line in std::io::BufReader::new(stdout)
            .lines()
            .map_while(Result::ok)
        {
            match parse_import_line(&line) {
                Some(ImportEvent::Done(path)) => outcome = Some(Ok(path)),
                Some(ImportEvent::Failed(why)) => outcome = Some(Err(why)),
                Some(event) => on_event(event),
                None => {}
            }
        }
    }
    on_event(ImportEvent::OutputEnded);
    let status = child.wait().map_err(|err| err.to_string())?;
    outcome.unwrap_or_else(|| Err(format!("vinowhisper-transcribe stopped ({status})")))
}

pub fn cancel_import(pid: u32) {
    use rustix::process::{Pid, Signal, kill_process};
    if let Some(pid) = i32::try_from(pid).ok().and_then(Pid::from_raw) {
        let _ = kill_process(pid, Signal::TERM);
    }
}

pub fn import_with_dialog(
    on_event: &mut dyn FnMut(ImportEvent),
) -> Result<Option<PathBuf>, String> {
    let file = match open_dialog() {
        Chosen::Path(file) => file,
        Chosen::Cancelled => return Ok(None),
        Chosen::NoPortal { reason: why } => {
            eprintln!("[vinowhisper-gui] no file dialog: {why}");
            return Err(
                "there's no file dialog on this desktop; run `vinowhisper-transcribe FILE` in a terminal"
                    .to_owned(),
            );
        }
    };
    let name = file
        .file_name()
        .map(|name| name.to_string_lossy().into_owned())
        .unwrap_or_default();
    on_event(ImportEvent::Chosen(name));
    import(&file, on_event).map(Some)
}

pub fn format_for(open_in: OpenIn) -> Format {
    match open_in {
        OpenIn::WordProcessor => Format::Odt,
        OpenIn::TextEditor => Format::Markdown,
    }
}

pub fn open_in_label(open_in: OpenIn) -> &'static str {
    match open_in {
        OpenIn::WordProcessor => "Word processor",
        OpenIn::TextEditor => "Text editor",
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Opened {
    pub exported: Exported,
    pub by_xdg_open: bool,
}

// A kept copy, not a temp file: people edit it, and `--reuse` never writes over it.
pub fn export_kept(request: &ExportRequest) -> Result<Exported, String> {
    let mut args = export_args(request, None);
    args.push("--reuse".as_ref());
    args.push("--json".as_ref());
    serde_json::from_str(run(&args)?.trim()).map_err(|_| "unreadable export result".to_owned())
}

#[derive(Debug, PartialEq, Eq)]
pub enum Launched {
    Portal,
    Cancelled,
    XdgOpen,
}

pub fn open_file(path: &Path) -> Result<Launched, String> {
    use ashpd::desktop::ResponseError;
    use ashpd::desktop::open_uri::OpenFileRequest;
    let file = std::fs::File::open(path).map_err(|err| format!("{}: {err}", path.display()))?;
    let asked = async_io::block_on(async {
        OpenFileRequest::default()
            .ask(true)
            .send_file(&std::os::fd::AsFd::as_fd(&file))
            .await?
            .response()
    });
    match asked {
        Ok(()) => Ok(Launched::Portal),
        Err(ashpd::Error::Response(ResponseError::Cancelled)) => Ok(Launched::Cancelled),
        Err(err) => {
            eprintln!("[vinowhisper-gui] no OpenURI portal ({err}); trying xdg-open");
            let mut child = Command::new("xdg-open")
                .arg(path)
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .spawn()
                .map_err(|err| format!("could not run xdg-open: {err}"))?;
            match exit_within(&mut child, XDG_OPEN_GRACE)? {
                Some(status) if !status.success() => {
                    Err(format!("xdg-open found no app for {}", path.display()))
                }
                Some(_) => Ok(Launched::XdgOpen),
                None => {
                    std::thread::spawn(move || child.wait());
                    Ok(Launched::XdgOpen)
                }
            }
        }
    }
}

// On a bare compositor xdg-open can wait for the app to exit, so still running after this
// counts as launched.
const XDG_OPEN_GRACE: std::time::Duration = std::time::Duration::from_secs(2);

pub(crate) fn exit_within(
    child: &mut std::process::Child,
    grace: std::time::Duration,
) -> Result<Option<std::process::ExitStatus>, String> {
    const POLL: std::time::Duration = std::time::Duration::from_millis(100);
    let deadline = std::time::Instant::now() + grace;
    loop {
        if let Some(status) = child.try_wait().map_err(|err| err.to_string())? {
            return Ok(Some(status));
        }
        if std::time::Instant::now() >= deadline {
            return Ok(None);
        }
        std::thread::sleep(POLL);
    }
}

pub fn open_in(request: &ExportRequest) -> Result<Option<Opened>, String> {
    let exported = export_kept(request)?;
    Ok(match open_file(&exported.path)? {
        Launched::Cancelled => None,
        launched => Some(Opened {
            exported,
            by_xdg_open: launched == Launched::XdgOpen,
        }),
    })
}

pub fn export_with_dialog(request: &ExportRequest) -> Result<Option<Exported>, String> {
    let suggested = export_path(request)?;
    match save_dialog(&suggested, request.format) {
        // The dialog has asked about replacing the file.
        Chosen::Path(path) => export(request, Some(&path), true).map(Some),
        Chosen::Cancelled => Ok(None),
        Chosen::NoPortal { reason: why } => {
            eprintln!(
                "[vinowhisper-gui] no save dialog ({why}); exporting to the documents folder"
            );
            let mut exported = export(request, None, false)?;
            exported.saved_without_dialog = true;
            Ok(Some(exported))
        }
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
    fn the_models_own_stamps_are_links_and_other_brackets_are_text() {
        assert_eq!(
            pieces("Nuclear weapons (0:02)"),
            [Piece::Text("Nuclear weapons ".into()), Piece::Stamp(2)]
        );
        assert_eq!(
            pieces("a (see below) [1:05]"),
            [
                Piece::Text("a (see below)".into()),
                Piece::Text(" ".into()),
                Piece::Stamp(65)
            ]
        );
    }

    #[test]
    fn summary_lines_are_drawn_as_what_they_mean_not_their_marks() {
        assert_eq!(
            summary_line("#### Key Point 1: Nuclear Weapons (0:02)"),
            SummaryLine::Heading("Key Point 1: Nuclear Weapons (0:02)".into())
        );
        assert_eq!(
            summary_line("### Summary:"),
            SummaryLine::Heading("Summary".into())
        );
        assert_eq!(
            summary_line("#hashtag"),
            SummaryLine::Text("#hashtag".into())
        );
        assert_eq!(
            summary_line("- a [00:02]"),
            SummaryLine::Bullet("a [00:02]".into())
        );
        assert_eq!(summary_line("  * b"), SummaryLine::Bullet("b".into()));
        assert_eq!(
            summary_line("_Machine-written summary._"),
            SummaryLine::Footer("Machine-written summary.".into())
        );
        assert_eq!(summary_line("   "), SummaryLine::Blank);
    }

    #[test]
    fn opening_at_a_stamp_lands_on_the_paragraph_holding_it() {
        let text = "vinoWhisper transcript, x\n\n[00:00] One.\n\n[01:10] Two.\n\n[61:05] Three.\n";
        let found = paragraphs(text);
        assert_eq!(found.len(), 4);
        assert_eq!(found[0].start_s, None);
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
        assert!(!listing.working());
        let entry = &listing.sessions[0];
        assert_eq!(entry.when(), "2026-10-27 14:05");
        assert_eq!(entry.length().unwrap(), "1 h 01 min");
        assert_eq!(entry.status(), "Summarised");
        assert!(parse_listing("Traceback").is_err());
    }

    #[test]
    fn the_session_being_summarised_says_so_and_the_rest_are_queued() {
        let listing = parse_listing(
            r#"{"current": "/t/b.vwsession", "queued": ["/t/a.vwsession"],
               "sessions": [{"path": "/t/b.vwsession", "summary": true, "title": "Old"},
                            {"path": "/t/a.vwsession"}, {"path": "/t/c.vwsession"}]}"#,
        )
        .unwrap();
        assert!(listing.working());
        let status: Vec<&str> = listing.sessions.iter().map(|e| listing.status(e)).collect();
        assert_eq!(
            status,
            [
                "Summarising…",
                "Queued for a summary",
                "Waiting for a summary"
            ]
        );
    }

    #[test]
    fn the_file_dialog_filters_by_extension_in_either_case_not_by_wildcard_mime() {
        let globs = media_globs();
        for wanted in ["*.mp3", "*.MP3", "*.mp4", "*.mkv", "*.wav", "*.M4A"] {
            assert!(globs.iter().any(|glob| glob == wanted), "{wanted}");
        }
        assert!(globs.iter().all(|glob| glob.starts_with("*.")));
    }

    #[test]
    fn without_ffprobe_an_import_knows_how_far_it_has_got_but_not_the_length() {
        assert_eq!(
            parse_import_line(r#"{"event": "Progress", "position_s": 15.3, "duration_s": null}"#),
            Some(ImportEvent::Progress {
                position_s: 15.3,
                duration_s: None
            })
        );
    }

    #[test]
    fn a_session_is_named_by_its_start_to_the_minute() {
        let entry = Entry {
            name: "a.vwsession".into(),
            started: Some("2026-10-27T14:05:12-04:00".into()),
            ..Entry::default()
        };
        assert_eq!(entry.when(), "2026-10-27 14:05");
    }

    #[test]
    fn a_session_without_a_full_start_time_is_named_by_its_file() {
        let entry = Entry {
            name: "2026-10-27-140512.vwsession".into(),
            started: Some("2026-10-27T14".into()),
            ..Entry::default()
        };
        assert_eq!(entry.when(), "2026-10-27-140512");
    }

    #[test]
    fn an_import_reads_what_vinowhisper_transcribe_prints() {
        assert_eq!(
            parse_import_line(
                r#"{"event": "Progress", "position_s": 15.3, "duration_s": 75.0, "elapsed_s": 0.1, "words": 8}"#
            ),
            Some(ImportEvent::Progress {
                position_s: 15.3,
                duration_s: Some(75.0)
            })
        );
        assert_eq!(
            parse_import_line(r#"{"event": "Done", "path": "/t/a.vwsession", "words": 32}"#),
            Some(ImportEvent::Done("/t/a.vwsession".into()))
        );
        assert_eq!(
            parse_import_line(r#"{"event": "Error", "message": "no speech found in a.mp3"}"#),
            Some(ImportEvent::Failed("no speech found in a.mp3".to_owned()))
        );
        assert_eq!(
            parse_import_line(r#"{"event": "Warning", "message": "the speech detector stopped"}"#),
            Some(ImportEvent::Warning(
                "the speech detector stopped".to_owned()
            ))
        );
        assert_eq!(
            parse_import_line(r#"{"event": "Ready", "device": "NPU"}"#),
            None
        );
        assert_eq!(parse_import_line("not json"), None);
    }

    #[test]
    fn a_file_uri_from_the_portal_is_a_local_path() {
        assert_eq!(
            file_uri_path("file:///home/me/My%20Notes%20%28draft%29.md"),
            Some(PathBuf::from("/home/me/My Notes (draft).md"))
        );
        assert_eq!(
            file_uri_path("file://localhost/t/caf%C3%A9.odt"),
            Some(PathBuf::from("/t/café.odt"))
        );
        assert_eq!(file_uri_path("file://host/share/a.md"), None);
        assert_eq!(file_uri_path("https://example.com/a.md"), None);
        assert_eq!(file_uri_path("file:///bad%zz"), None);
    }

    #[test]
    fn an_export_reads_what_vinowhisper_session_prints() {
        let exported: Exported = serde_json::from_str(
            r#"{"path": "/d/2026-10-27 Transcript.md", "parts": ["transcript"],
               "summary_missing": true, "state": "written"}"#,
        )
        .unwrap();
        assert_eq!(exported.path, PathBuf::from("/d/2026-10-27 Transcript.md"));
        assert!(exported.summary_missing && !exported.saved_without_dialog);
        let request = ExportRequest {
            path: "/t/a.vwsession".into(),
            scope: Scope::from(Part::Summary),
            format: Format::Odt,
        };
        let args: Vec<_> = export_args(&request, Some(Path::new("/d/x.odt")))
            .into_iter()
            .map(|a| a.to_string_lossy().into_owned())
            .collect();
        assert_eq!(
            args,
            [
                "export",
                "/t/a.vwsession",
                "--part",
                "summary",
                "--format",
                "odt",
                "-o",
                "/d/x.odt"
            ]
        );
    }
}
