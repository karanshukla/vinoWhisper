use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::time::Duration;

use futures_lite::StreamExt;
use serde::Deserialize;
use zbus::zvariant::Value;

use crate::APP_ID;

const READY_RELPATH: &str = "vinowhisper/summaries-ready.json";
// How long an unanswered notification's "Open" stays wired up.
const LISTEN_FOR: Duration = Duration::from_secs(30 * 60);

#[derive(Debug, Clone, PartialEq, Deserialize)]
pub struct Ready {
    pub path: PathBuf,
    #[serde(default)]
    pub title: Option<String>,
    #[serde(default)]
    pub failed: bool,
}

#[derive(Debug, PartialEq)]
pub struct Notice {
    pub summary: String,
    pub body: String,
    pub settings_args: Vec<String>,
}

fn ready_path() -> Option<PathBuf> {
    std::env::var_os("XDG_RUNTIME_DIR")
        .filter(|dir| !dir.is_empty())
        .map(|dir| PathBuf::from(dir).join(READY_RELPATH))
}

pub fn take_ready(path: &Path) -> Vec<Ready> {
    let Ok(text) = std::fs::read_to_string(path) else {
        return Vec::new();
    };
    let _ = std::fs::remove_file(path);
    serde_json::from_str(&text).unwrap_or_default()
}

const MAX_LABEL_CHARS: usize = 120;

// The title is model-written from untrusted captions, and notification bodies may be markup.
fn plain(text: &str) -> String {
    let mut out = String::new();
    for (count, ch) in text.chars().filter(|ch| !ch.is_control()).enumerate() {
        if count == MAX_LABEL_CHARS {
            out.push('…');
            break;
        }
        match ch {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            _ => out.push(ch),
        }
    }
    out
}

fn label(ready: &Ready) -> String {
    plain(&ready.title.clone().unwrap_or_else(|| {
        ready
            .path
            .file_stem()
            .map(|stem| stem.to_string_lossy().into_owned())
            .unwrap_or_default()
    }))
}

fn open_args(target: Option<String>) -> Vec<String> {
    match target {
        Some(target) => vec!["--open".to_owned(), target],
        None => vec!["--sessions".to_owned()],
    }
}

pub fn notice(ready: &[Ready]) -> Option<Notice> {
    let (done, failed): (Vec<&Ready>, Vec<&Ready>) = ready.iter().partition(|r| !r.failed);
    let failed_line = match failed.len() {
        0 => None,
        1 => Some(format!(
            "Could not summarise {}; open it to try again.",
            label(failed[0])
        )),
        n => Some(format!(
            "{n} sessions could not be summarised; open Sessions to try again."
        )),
    };
    match (done.as_slice(), failed.as_slice()) {
        ([], []) => None,
        ([one], []) => Some(Notice {
            summary: "Summary ready".to_owned(),
            body: label(one),
            settings_args: open_args(Some(format!("{}#summary", one.path.display()))),
        }),
        ([], [one]) => Some(Notice {
            summary: "Summary failed".to_owned(),
            body: failed_line.unwrap_or_default(),
            settings_args: open_args(Some(one.path.display().to_string())),
        }),
        ([], _) => Some(Notice {
            summary: "Summaries failed".to_owned(),
            body: failed_line.unwrap_or_default(),
            settings_args: open_args(None),
        }),
        (done, _) => {
            let mut lines: Vec<String> = done.iter().take(3).map(|r| label(r)).collect();
            if done.len() > 3 {
                lines.push(format!("and {} more", done.len() - 3));
            }
            lines.extend(failed_line);
            Some(Notice {
                summary: format!(
                    "{} summar{} ready",
                    done.len(),
                    if done.len() == 1 { "y" } else { "ies" }
                ),
                body: lines.join("\n"),
                settings_args: open_args(None),
            })
        }
    }
}

pub fn announce_ready(open: fn(&[String])) {
    let Some(path) = ready_path() else {
        return;
    };
    let Some(notice) = notice(&take_ready(&path)) else {
        return;
    };
    let spawned = std::thread::Builder::new()
        .name("summaries".into())
        .spawn(move || {
            if let Err(err) = async_io::block_on(send(&notice, open)) {
                eprintln!("[vinowhisper-tray] could not send the summary notification: {err}");
            }
        });
    if let Err(err) = spawned {
        eprintln!("[vinowhisper-tray] could not send the summary notification: {err}");
    }
}

async fn send(notice: &Notice, open: fn(&[String])) -> zbus::Result<()> {
    let session = zbus::Connection::session().await?;
    let rule = zbus::MatchRule::builder()
        .msg_type(zbus::message::Type::Signal)
        .interface("org.freedesktop.Notifications")?
        .build();
    // Subscribed before Notify, so a quick click is not missed.
    let mut signals = zbus::MessageStream::for_match_rule(rule, &session, Some(8)).await?;
    let hints: HashMap<&str, Value> = HashMap::from([
        ("desktop-entry", Value::from(APP_ID)),
        ("category", Value::from("transfer.complete")),
    ]);
    let reply = session
        .call_method(
            Some("org.freedesktop.Notifications"),
            "/org/freedesktop/Notifications",
            Some("org.freedesktop.Notifications"),
            "Notify",
            &(
                "vinoWhisper",
                0u32,
                APP_ID,
                notice.summary.as_str(),
                notice.body.as_str(),
                vec!["default", "Open", "open", "Open"],
                hints,
                -1i32,
            ),
        )
        .await?;
    let id: u32 = reply.body().deserialize()?;
    let deadline = async_io::Timer::after(LISTEN_FOR);
    let wait = async {
        while let Some(Ok(message)) = signals.next().await {
            let header = message.header();
            match header.member().map(|m| m.as_str()) {
                Some("ActionInvoked") => {
                    if let Ok((signalled, _action)) = message.body().deserialize::<(u32, String)>()
                        && signalled == id
                    {
                        open(&notice.settings_args);
                        return;
                    }
                }
                Some("NotificationClosed") => {
                    if let Ok((signalled, _reason)) = message.body().deserialize::<(u32, u32)>()
                        && signalled == id
                    {
                        return;
                    }
                }
                _ => {}
            }
        }
    };
    futures_lite::future::or(wait, async {
        deadline.await;
    })
    .await;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ready(path: &str, title: Option<&str>, failed: bool) -> Ready {
        Ready {
            path: PathBuf::from(path),
            title: title.map(str::to_owned),
            failed,
        }
    }

    #[test]
    fn one_summary_opens_that_summary() {
        let notice =
            notice(&[ready("/t/2026-10-27-140512.vwsession", Some("Talk"), false)]).unwrap();
        assert_eq!(notice.summary, "Summary ready");
        assert_eq!(notice.body, "Talk");
        assert_eq!(
            notice.settings_args,
            ["--open", "/t/2026-10-27-140512.vwsession#summary"]
        );
    }

    #[test]
    fn a_backfill_is_one_notification_that_opens_the_sessions_window() {
        let many: Vec<Ready> = (0..5)
            .map(|i| ready(&format!("/t/{i}.vwsession"), Some(&format!("S{i}")), false))
            .collect();
        let notice = notice(&many).unwrap();
        assert_eq!(notice.summary, "5 summaries ready");
        assert_eq!(notice.body, "S0\nS1\nS2\nand 2 more");
        assert_eq!(notice.settings_args, ["--sessions"]);
    }

    #[test]
    fn an_untitled_session_is_named_by_its_file() {
        let notice = notice(&[ready("/t/2026-10-27-140512.vwsession", None, false)]).unwrap();
        assert_eq!(notice.body, "2026-10-27-140512");
    }

    #[test]
    fn a_failure_says_so_and_opens_the_session() {
        let notice = notice(&[ready("/t/a.vwsession", None, true)]).unwrap();
        assert_eq!(notice.summary, "Summary failed");
        assert!(notice.body.contains("try again"));
        assert_eq!(notice.settings_args, ["--open", "/t/a.vwsession"]);
    }

    #[test]
    fn nothing_ready_is_no_notification() {
        assert_eq!(notice(&[]), None);
    }

    #[test]
    fn the_ready_file_is_read_once() {
        let path = std::env::temp_dir().join(format!("vwg-{}-ready.json", std::process::id()));
        std::fs::write(
            &path,
            r#"[{"path": "/t/a.vwsession", "title": "A", "failed": false}]"#,
        )
        .unwrap();
        assert_eq!(
            take_ready(&path),
            [ready("/t/a.vwsession", Some("A"), false)]
        );
        assert!(take_ready(&path).is_empty());
    }

    #[test]
    fn a_title_cannot_carry_markup_or_run_on() {
        let notice = notice(&[ready(
            "/t/a.vwsession",
            Some("<a href='x'>click</a> & \u{7}more"),
            false,
        )])
        .unwrap();
        assert_eq!(notice.body, "&lt;a href='x'&gt;click&lt;/a&gt; &amp; more");
        let long = "x".repeat(500);
        let notice = super::notice(&[ready("/t/a.vwsession", Some(&long), false)]).unwrap();
        assert_eq!(notice.body.chars().count(), MAX_LABEL_CHARS + 1);
    }
}
