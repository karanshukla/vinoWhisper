use std::path::PathBuf;
use std::process::{Command, Stdio};

use serde::Deserialize;
use vinowhisper_settings::EchoCancel;

#[derive(Debug, PartialEq, Deserialize)]
struct Status {
    reason: String,
}

/// What `--echo-cancel <mode>` would do on the current output, in the words the page shows.
pub fn status(mode: EchoCancel) -> Result<String, String> {
    let program = command();
    let output = Command::new(&program)
        .args([
            "--source",
            "both",
            "--echo-status",
            "--echo-cancel",
            mode.as_arg(),
        ])
        .stdin(Stdio::null())
        .stderr(Stdio::null())
        .output()
        .map_err(|err| format!("could not run vinowhisper-caption: {err}"))?;
    let text = String::from_utf8_lossy(&output.stdout);
    parse(text.lines().last().unwrap_or(""))
}

fn command() -> PathBuf {
    std::env::current_exe()
        .ok()
        .and_then(|exe| exe.parent().map(|dir| dir.join("vinowhisper-caption")))
        .filter(|path| path.is_file())
        .unwrap_or_else(|| PathBuf::from("vinowhisper-caption"))
}

fn parse(line: &str) -> Result<String, String> {
    let status: Status = serde_json::from_str(line.trim())
        .map_err(|err| format!("vinowhisper-caption said something unexpected: {err}"))?;
    Ok(format!("Auto has it {}.", status.reason))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn what_auto_chose_reads_as_a_sentence_with_its_reason() {
        assert_eq!(
            parse(r#"{"mode": "auto", "active": false, "reason": "off: Bluetooth"}"#),
            Ok("Auto has it off: Bluetooth.".to_owned())
        );
    }

    #[test]
    fn output_that_is_not_json_is_an_error_not_a_blank_line() {
        assert!(parse("").is_err());
    }
}
