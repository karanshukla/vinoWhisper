//! The pill is a click-through layer surface with no accessibility tree, so a screen reader
//! never hears it (#98). Failures and warnings, the states that need hearing, go out as a
//! desktop notification too, which screen readers read aloud. Only while one is running:
//! everyone else already sees the pill. Never the dictated words, only the message.

use std::collections::HashMap;

use zbus::zvariant::Value;

use crate::APP_ID;

pub fn announce(message: String) {
    let spawned = std::thread::Builder::new()
        .name("announce".into())
        .spawn(move || {
            if let Err(err) = async_io::block_on(send(&message)) {
                eprintln!("[vinowhisper-tray] could not announce on D-Bus: {err}");
            }
        });
    if let Err(err) = spawned {
        eprintln!("[vinowhisper-tray] could not announce: {err}");
    }
}

async fn send(message: &str) -> zbus::Result<()> {
    let session = zbus::Connection::session().await?;
    if !screen_reader_on(&session).await {
        return Ok(());
    }
    let hints: HashMap<&str, Value> = HashMap::from([
        ("urgency", Value::U8(1)),
        ("transient", Value::Bool(true)),
        ("desktop-entry", Value::from(APP_ID)),
    ]);
    session
        .call_method(
            Some("org.freedesktop.Notifications"),
            "/org/freedesktop/Notifications",
            Some("org.freedesktop.Notifications"),
            "Notify",
            &(
                "vinoWhisper",
                0u32,
                APP_ID,
                "Dictation",
                message,
                Vec::<&str>::new(),
                hints,
                -1i32,
            ),
        )
        .await?;
    Ok(())
}

// Set by Orca when it starts, and by GNOME's and Plasma's screen reader switches.
async fn screen_reader_on(session: &zbus::Connection) -> bool {
    let reply = session
        .call_method(
            Some("org.a11y.Bus"),
            "/org/a11y/bus",
            Some("org.freedesktop.DBus.Properties"),
            "Get",
            &("org.a11y.Status", "ScreenReaderEnabled"),
        )
        .await;
    reply
        .ok()
        .and_then(|reply| {
            reply
                .body()
                .deserialize::<zbus::zvariant::OwnedValue>()
                .ok()
        })
        .and_then(|value| bool::try_from(value).ok())
        .unwrap_or(false)
}
