//! The API key lives in the desktop's Secret Service, reached through `secret-tool` so this
//! binary needs no D-Bus client. It is read from stdin and never put in an argument list,
//! where any local user could see it in `ps`.

use std::io::Write;
use std::process::{Command, Stdio};

use vinowhisper_settings::SECRET_ATTRIBUTES;

const LABEL: &str = "vinoWhisper API key";

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Key {
    Checking,
    Stored,
    Missing,
    /// No `secret-tool` on this machine, so nothing here can store a key.
    NoTool,
    Failed(String),
}

fn tool() -> Command {
    Command::new("secret-tool")
}

fn lookup_args() -> Vec<&'static str> {
    ["lookup"].into_iter().chain(SECRET_ATTRIBUTES).collect()
}

fn store_args() -> Vec<&'static str> {
    ["store", "--label", LABEL]
        .into_iter()
        .chain(SECRET_ATTRIBUTES)
        .collect()
}

fn clear_args() -> Vec<&'static str> {
    ["clear"].into_iter().chain(SECRET_ATTRIBUTES).collect()
}

fn missing_tool(err: &std::io::Error) -> bool {
    err.kind() == std::io::ErrorKind::NotFound
}

/// Whether a key is stored. The value is read to find out and dropped, never kept.
pub fn status() -> Key {
    match tool()
        .args(lookup_args())
        .stdin(Stdio::null())
        .stderr(Stdio::null())
        .output()
    {
        Ok(output) if output.status.success() && !output.stdout.is_empty() => Key::Stored,
        Ok(_) => Key::Missing,
        Err(err) if missing_tool(&err) => Key::NoTool,
        Err(err) => Key::Failed(err.to_string()),
    }
}

pub fn store(key: &str) -> Key {
    let spawned = tool()
        .args(store_args())
        .stdin(Stdio::piped())
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .spawn();
    let mut child = match spawned {
        Ok(child) => child,
        Err(err) if missing_tool(&err) => return Key::NoTool,
        Err(err) => return Key::Failed(err.to_string()),
    };
    if let Some(mut stdin) = child.stdin.take() {
        // Closing it is what tells secret-tool the value has ended.
        if let Err(err) = stdin.write_all(key.as_bytes()) {
            return Key::Failed(err.to_string());
        }
    }
    match child.wait_with_output() {
        Ok(output) if output.status.success() => Key::Stored,
        Ok(output) => Key::Failed(failure(&output.stderr, "the wallet refused the key")),
        Err(err) => Key::Failed(err.to_string()),
    }
}

pub fn clear() -> Key {
    match tool()
        .args(clear_args())
        .stdin(Stdio::null())
        .stderr(Stdio::piped())
        .output()
    {
        Ok(output) if output.status.success() => Key::Missing,
        Ok(output) => Key::Failed(failure(&output.stderr, "the wallet refused")),
        Err(err) if missing_tool(&err) => Key::NoTool,
        Err(err) => Key::Failed(err.to_string()),
    }
}

fn failure(stderr: &[u8], fallback: &str) -> String {
    let text = String::from_utf8_lossy(stderr);
    let first = text.lines().next().unwrap_or("").trim();
    if first.is_empty() { fallback } else { first }.to_owned()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_key_is_stored_under_the_entry_the_dictation_process_looks_up() {
        let tail = ["service", "vinowhisper", "account", "anthropic-api-key"];
        assert_eq!(
            lookup_args(),
            ["lookup"].into_iter().chain(tail).collect::<Vec<_>>()
        );
        assert_eq!(&store_args()[3..], tail);
        assert_eq!(clear_args()[0], "clear");
    }

    #[test]
    fn no_argument_is_ever_the_key() {
        // The value reaches secret-tool on stdin only; there is no argv slot for it.
        for args in [lookup_args(), store_args(), clear_args()] {
            assert!(args.iter().all(|arg| !arg.starts_with("sk-")));
        }
        assert!(!store_args().contains(&"--password"));
    }

    #[test]
    fn a_failure_shows_the_tools_own_first_line() {
        assert_eq!(
            failure(b"Cannot create an item in a locked collection\nmore", "x"),
            "Cannot create an item in a locked collection"
        );
        assert_eq!(failure(b"", "the wallet refused"), "the wallet refused");
    }
}
