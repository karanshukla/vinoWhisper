
use super::*;
use eframe::egui::accesskit::Role;
use egui_kittest::Harness;
use egui_kittest::kittest::{NodeT, Queryable};

// Nothing is listed, read or spawned: `show` never refreshes, and no test presses a button
// that runs vinowhisper-session.
struct Under {
    window: SessionsWindow,
    styled: bool,
}

fn harness(save_transcripts: bool) -> Harness<'static, Under> {
    Harness::builder().with_size((820.0, 760.0)).build_ui_state(
        |ui, under: &mut Under| {
            let ctx = &ui.ctx().clone();
            if !under.styled {
                theme::apply(ctx);
                under.styled = true;
                ctx.request_repaint();
                return;
            }
            under.window.show(ui);
        },
        Under {
            window: SessionsWindow {
                // Never the user's gui.json.
                settings_path: scratch_settings(),
                ..SessionsWindow::with(save_transcripts)
            },
            styled: false,
        },
    )
}

fn scratch_settings() -> PathBuf {
    static N: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);
    let n = N.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
    std::env::temp_dir()
        .join(format!("vinowhisper-gui-test-{}-{n}", std::process::id()))
        .join("gui.json")
}

fn listing() -> Listing {
    sessions::parse_listing(
            r#"{"directory": "/t", "auto": true, "model_ready": false, "backfill": 3,
               "sessions": [
                 {"path": "/t/b.vwsession", "name": "b.vwsession", "started": "2026-10-27T14:05:12-04:00",
                  "duration_s": 2530, "source": "system audio", "summary": true, "title": "Local captions talk",
                  "playing": "How NPUs work (Firefox)",
                  "verified": true},
                 {"path": "/t/a.vwsession", "name": "a.vwsession", "started": "2026-10-26T09:00:00-04:00",
                  "duration_s": 40, "source": "microphone", "summary": false, "verified": true}]}"#,
        )
        .unwrap()
}

#[test]
fn the_window_is_named() {
    let mut harness = harness(true);
    harness.run();
    harness.get_by_role_and_label(Role::Heading, "Saved sessions");
    assert_eq!(
        harness.root().accesskit_node().label().as_deref(),
        Some(TITLE)
    );
    harness.get_by_role_and_label(Role::Button, "Settings");
}

#[test]
fn the_app_s_title_bar_puts_the_window_buttons_in_the_list_and_a_session() {
    for app_title_bar in [true, false] {
        let mut harness = harness(true);
        harness.state_mut().window.app_title_bar = app_title_bar;
        harness.state_mut().window.state.lock().unwrap().listing = Some(Ok(listing()));
        harness.run();
        let buttons = |harness: &Harness<'static, Under>| {
            ["Minimise", "Maximise", "Close"].iter().all(|name| {
                harness
                    .query_by_role_and_label(Role::Button, name)
                    .is_some()
            })
        };
        assert_eq!(buttons(&harness), app_title_bar);
        harness.get_by_role_and_label(Role::Button, "Settings");
        harness.state_mut().window.state.lock().unwrap().viewer = Viewer {
            target: Some(Target::parse("/t/b.vwsession#transcript")),
            text: Some(Ok("Hello.".into())),
            ..Viewer::default()
        };
        harness.run();
        assert_eq!(buttons(&harness), app_title_bar);
        harness.get_by_role_and_label(Role::Button, "All sessions");
    }
}

#[test]
fn each_session_is_listed_with_what_it_can_do() {
    let mut harness = harness(false);
    harness.state_mut().window.state.lock().unwrap().listing = Some(Ok(listing()));
    harness.run();
    harness.get_by_label(
        "The summary model isn't downloaded yet; get it on the Cleanup page of the settings.",
    );
    harness.get_by_role_and_label(Role::Button, "Open settings");
    harness.get_by_role_and_label(Role::Button, "Bundle and summarise 3 transcripts");
    harness.get_by_role_and_label(Role::Heading, "Sessions (2)");
    harness.get_by_role_and_label(Role::Heading, "Local captions talk");
    harness.get_by_label("2026-10-27 14:05 · 42 min · system audio · How NPUs work (Firefox)");
    harness.get_by_role_and_label(Role::Heading, "2026-10-26 09:00");
    harness.get_by_label("40 s · microphone");
    harness.get_by_label("Waiting for a summary");
    assert_eq!(
        harness
            .query_all_by_role_and_label(Role::Button, "Transcript")
            .count(),
        2
    );
    // A summary to read, or one to ask for; the rest waits behind More.
    harness.get_by_role_and_label(Role::Button, "Summary");
    harness.get_by_role_and_label(Role::Button, "Summarise");
    assert_eq!(
        harness
            .query_all_by_role_and_label(Role::Button, "More")
            .count(),
        2
    );
    assert!(harness.query_by_label("Delete summary").is_none());
    harness.get_by_label(
        "Saving transcripts is off (settings, Captions page), so new sessions aren't kept.",
    );
}

#[test]
fn deleting_asks_in_the_card_and_cancel_backs_out() {
    let mut harness = harness(true);
    harness.state_mut().window.state.lock().unwrap().listing = Some(Ok(listing()));
    harness.state_mut().window.confirm_delete = Some("/t/b.vwsession".into());
    harness.run();
    harness.get_by_label("Delete this session, transcript and summary?");
    harness.get_by_role_and_label(Role::Button, "Delete the session");
    harness
        .get_by_role_and_label(Role::Button, "Cancel")
        .click();
    harness.run();
    assert!(harness.state().window.confirm_delete.is_none());
    assert!(
        harness
            .query_by_label("Delete this session, transcript and summary?")
            .is_none()
    );
}

#[test]
fn a_summary_opens_with_its_stamps_as_links_and_says_a_model_wrote_it() {
    let mut harness = harness(true);
    {
        let mut state = harness.state_mut().window.state.lock().unwrap();
        state.listing = Some(Ok(listing()));
        state.viewer = Viewer {
                target: Some(Target::parse("/t/b.vwsession#summary")),
                text: Some(Ok(
                    "# Local captions talk\n\nAn overview.\n\n#### Key point (0:02)\n- Runs locally [03:10]\n\n_Machine-written summary._\n".into(),
                )),
                ..Viewer::default()
            };
    }
    harness.run();
    harness.get_by_role_and_label(Role::Button, "All sessions");
    harness.get_by_role_and_label(Role::Button, "Show in folder");
    // Once, as the viewer's heading, not again from the summary's own title.
    assert_eq!(
        harness
            .query_all_by_role_and_label(Role::Heading, "Local captions talk")
            .count(),
        1
    );
    harness.get_by_role_and_label(Role::Heading, "Key point (0:02)");
    harness.get_by_role_and_label(Role::Link, "[00:02]");
    harness.get_by_role_and_label(Role::Link, "[03:10]");
    harness.get_by_label("Machine-written summary.");
    let tab = harness.get_by_role_and_label(Role::Tab, "Summary");
    assert_eq!(tab.accesskit_node().is_selected(), Some(true));
}

#[test]
fn a_transcript_opened_at_a_stamp_marks_that_paragraph() {
    let mut harness = harness(true);
    harness.state_mut().window.state.lock().unwrap().viewer = Viewer {
        target: Some(Target::parse("/t/b.vwsession#transcript@01:30")),
        text: Some(Ok(
            "vinoWhisper transcript, x\n\n[00:00] One.\n\n[01:10] Two.\n".into(),
        )),
        ..Viewer::default()
    };
    harness.run();
    harness.get_by_label("[01:10] Two.");
    assert!(
        harness
            .query_by_label("vinoWhisper transcript, x")
            .is_none()
    );
    assert!(harness.state().window.state.lock().unwrap().viewer.scrolled);
}

#[test]
fn a_session_being_summarised_says_so_and_cannot_be_asked_again() {
    let mut harness = harness(true);
    let mut working = listing();
    working.current = Some("/t/a.vwsession".into());
    working.queued = vec!["/t/b.vwsession".into()];
    harness.state_mut().window.state.lock().unwrap().listing = Some(Ok(working));
    harness.run_steps(2);
    harness.get_by_label("Summarising…");
    harness.get_by_label("Queued for a summary");
    let button = harness.get_by_role_and_label(Role::Button, "Summarise");
    assert!(button.accesskit_node().is_disabled());
}

fn reading(fragment: &str, text: &str) -> Viewer {
    Viewer {
        target: Some(Target::parse(&format!("/t/b.vwsession#{fragment}"))),
        text: Some(Ok(text.into())),
        ..Viewer::default()
    }
}

// The exporter runs on its own thread; step until its answer is in.
fn until_exported(harness: &mut Harness<'static, Under>) {
    for _ in 0..500 {
        harness.step();
        if harness
            .state()
            .window
            .state
            .lock()
            .unwrap()
            .exported
            .is_some()
        {
            harness.run();
            return;
        }
        std::thread::sleep(Duration::from_millis(5));
    }
    panic!("the export never finished");
}

fn exporter_answering(
    harness: &mut Harness<'static, Under>,
    answer: Result<Option<Exported>, String>,
) -> Arc<Mutex<Vec<ExportRequest>>> {
    let seen = Arc::new(Mutex::new(Vec::new()));
    let recorded = Arc::clone(&seen);
    harness.state_mut().window.exporter = Arc::new(move |request| {
        recorded.lock().unwrap().push(request.clone());
        answer.clone()
    });
    seen
}

fn is_on(harness: &Harness<'static, Under>, label: &str) -> bool {
    harness
        .get_by_role_and_label(Role::RadioButton, label)
        .accesskit_node()
        .toggled()
        == Some(egui::accesskit::Toggled::True)
}

#[test]
fn export_in_the_viewer_follows_the_tab_and_says_where_it_went() {
    let mut harness = harness(true);
    let seen = exporter_answering(
        &mut harness,
        Ok(Some(Exported {
            path: "/d/2026-10-27 Local captions talk.odt".into(),
            state: "written".into(),
            ..Exported::default()
        })),
    );
    {
        let mut state = harness.state_mut().window.state.lock().unwrap();
        state.listing = Some(Ok(listing()));
        state.viewer = reading("summary", "# Local captions talk\n\nText.\n");
    }
    harness.run();
    harness
        .get_by_role_and_label(Role::Button, "Export…")
        .click();
    harness.run();
    harness.get_by_role_and_label(Role::Heading, "Export");
    assert!(is_on(&harness, "Summary"));
    assert!(is_on(&harness, "Markdown"));
    harness
        .get_by_role_and_label(Role::RadioButton, "Document (.odt)")
        .click();
    harness.run();
    harness.get_by_label("For LibreOffice, Word and Google Docs.");
    harness.get_by_role_and_label(Role::Button, "Save…").click();
    until_exported(&mut harness);
    assert_eq!(
        *seen.lock().unwrap(),
        [ExportRequest {
            path: "/t/b.vwsession".into(),
            scope: Scope::Summary,
            format: Format::Odt,
        }]
    );
    assert!(harness.state().window.export_dialog.is_none());
    harness.get_by_label("Exported to /d/2026-10-27 Local captions talk.odt.");
    // The header's and the notice's.
    assert_eq!(
        harness
            .query_all_by_role_and_label(Role::Button, "Show in folder")
            .count(),
        2
    );
    harness
        .get_by_role_and_label(Role::Button, "Dismiss")
        .click();
    harness.run();
    assert!(
        harness
            .state()
            .window
            .state
            .lock()
            .unwrap()
            .exported
            .is_none()
    );
}

#[test]
fn export_from_a_card_offers_only_the_transcript_without_a_summary_and_cancels() {
    let mut harness = harness(true);
    let seen = exporter_answering(&mut harness, Ok(None));
    harness.state_mut().window.state.lock().unwrap().listing = Some(Ok(listing()));
    harness.run();
    // The second card has no summary.
    harness
        .query_all_by_role_and_label(Role::Button, "More")
        .nth(1)
        .unwrap()
        .click();
    harness.run();
    harness.get_by_label("Export…").click();
    harness.run();
    harness.get_by_label("The transcript: this session has no summary yet.");
    assert!(
        harness
            .query_by_role_and_label(Role::RadioButton, "Summary")
            .is_none()
    );
    let dialog = harness.state().window.export_dialog.clone().unwrap();
    assert_eq!(dialog.path, PathBuf::from("/t/a.vwsession"));
    assert_eq!(dialog.scope, Scope::Transcript);
    harness
        .get_by_role_and_label(Role::Button, "Cancel")
        .click();
    harness.run();
    assert!(harness.state().window.export_dialog.is_none());
    assert!(seen.lock().unwrap().is_empty());

    // A card with a summary starts on both.
    harness
        .query_all_by_role_and_label(Role::Button, "More")
        .next()
        .unwrap()
        .click();
    harness.run();
    harness.get_by_label("Export…").click();
    harness.run();
    assert!(is_on(&harness, "Both"));
}

#[test]
fn without_a_save_dialog_the_notice_says_where_it_went_and_what_was_left_out() {
    let mut harness = harness(true);
    exporter_answering(
        &mut harness,
        Ok(Some(Exported {
            path: "/home/me/Documents/vinoWhisper/2026-10-26 Transcript.md".into(),
            summary_missing: true,
            fallback: true,
            ..Exported::default()
        })),
    );
    harness.state_mut().window.state.lock().unwrap().listing = Some(Ok(listing()));
    harness.state_mut().window.export_dialog = Some(ExportDialog {
        path: "/t/a.vwsession".into(),
        scope: Scope::Both,
        format: Format::Markdown,
        has_summary: true,
    });
    harness.run();
    harness.get_by_role_and_label(Role::Button, "Save…").click();
    until_exported(&mut harness);
    harness.get_by_label(
        "There's no save dialog on this desktop, so it's in \
             /home/me/Documents/vinoWhisper/2026-10-26 Transcript.md. \
             The transcript only: there's no summary yet.",
    );
    harness.get_by_role_and_label(Role::Button, "Show in folder");
}

#[test]
fn a_failed_export_says_why_and_a_cancelled_one_says_nothing() {
    let mut harness = harness(true);
    exporter_answering(&mut harness, Err("/d/x.md already exists".into()));
    harness.state_mut().window.state.lock().unwrap().listing = Some(Ok(listing()));
    let dialog = ExportDialog {
        path: "/t/a.vwsession".into(),
        scope: Scope::Transcript,
        format: Format::Markdown,
        has_summary: false,
    };
    harness.state_mut().window.export_dialog = Some(dialog.clone());
    harness.run();
    harness.get_by_role_and_label(Role::Button, "Save…").click();
    until_exported(&mut harness);
    harness.get_by_label("Export didn't work: /d/x.md already exists");

    exporter_answering(&mut harness, Ok(None));
    harness.state_mut().window.export_dialog = Some(dialog);
    harness.run();
    harness.get_by_role_and_label(Role::Button, "Save…").click();
    for _ in 0..500 {
        harness.step();
        if !harness.state().window.state.lock().unwrap().exporting {
            break;
        }
        std::thread::sleep(Duration::from_millis(5));
    }
    harness.run();
    assert!(
        harness
            .state()
            .window
            .state
            .lock()
            .unwrap()
            .exported
            .is_none()
    );
    assert!(
        harness
            .query_by_label_contains("Export didn't work")
            .is_none()
    );
}

#[test]
fn copy_puts_the_part_on_screen_on_the_clipboard() {
    let text = "vinoWhisper transcript, x\n\n[00:00] One.\n";
    let mut harness = harness(true);
    harness.state_mut().window.state.lock().unwrap().viewer = reading("transcript", text);
    harness.run();
    harness.get_by_role_and_label(Role::Button, "Copy").click();
    harness.step();
    assert!(
        harness
            .output()
            .platform_output
            .commands
            .contains(&egui::OutputCommand::CopyText(text.into()))
    );
    harness.run();
    harness.get_by_role_and_label(Role::Button, "Copied");

    // Nothing to copy until the text is in.
    harness.state_mut().window.state.lock().unwrap().viewer = Viewer {
        text: None,
        ..reading("transcript", text)
    };
    harness.run();
    let copy = harness.get_by_role_and_label(Role::Button, "Copy");
    assert!(copy.accesskit_node().is_disabled());
}

// A stand-in for vinoSpeak#42's `vinospeak --file BUNDLE --part PART`.
fn fake_speaker(body: &str) -> PathBuf {
    use std::os::unix::fs::PermissionsExt;
    static N: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);
    let n = N.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
    let dir = std::env::temp_dir().join(format!(
        "vinowhisper-fake-vinospeak-{}-{n}",
        std::process::id()
    ));
    std::fs::create_dir_all(&dir).unwrap();
    let path = dir.join("vinospeak");
    let script = format!("#!/bin/sh\ncd '{}'\n{body}\n", dir.display());
    std::fs::write(&path, script).unwrap();
    std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755)).unwrap();
    path
}

fn viewing_summary(harness: &mut Harness<'static, Under>) {
    let mut state = harness.state_mut().window.state.lock().unwrap();
    state.listing = Some(Ok(listing()));
    state.viewer = Viewer {
        target: Some(Target::parse("/t/b.vwsession#summary")),
        text: Some(Ok("# Local captions talk\n\nText.\n".into())),
        ..Viewer::default()
    };
}

fn until(harness: &mut Harness<'static, Under>, done: impl Fn(&Under) -> bool) {
    for _ in 0..400 {
        harness.step();
        if done(harness.state()) {
            harness.run();
            return;
        }
        std::thread::sleep(Duration::from_millis(10));
    }
    panic!("gave up waiting");
}

#[test]
fn read_aloud_is_not_offered_without_vinospeak() {
    let mut harness = harness(true);
    viewing_summary(&mut harness);
    harness.run();
    assert!(harness.query_by_label("Read aloud").is_none());
    harness.state_mut().window.state.lock().unwrap().viewer = Viewer::default();
    harness.run();
    harness
        .query_all_by_role_and_label(Role::Button, "More")
        .next()
        .unwrap()
        .click();
    harness.run();
    harness.get_by_label("Show in folder");
    assert!(harness.query_by_label("Read aloud").is_none());
}

#[test]
fn read_aloud_turns_to_stop_while_it_reads_and_back_when_it_ends() {
    let speaker = fake_speaker("echo \"$@\" > args\nsleep 0.5");
    let args = speaker.with_file_name("args");
    let mut harness = harness(true);
    harness.state_mut().window.speaker = Some(speaker);
    viewing_summary(&mut harness);
    harness.run();
    harness
        .get_by_role_and_label(Role::Button, "Read aloud")
        .click();
    harness.run();
    harness.get_by_role_and_label(Role::Button, "Stop");
    until(&mut harness, |under| under.window.reading.is_none());
    harness.get_by_role_and_label(Role::Button, "Read aloud");
    assert_eq!(
        std::fs::read_to_string(&args).unwrap(),
        "--file /t/b.vwsession --part summary\n"
    );
    assert!(harness.state().window.read_error.is_none());
}

#[test]
fn stop_ends_the_reading_and_whatever_it_started() {
    let speaker = fake_speaker("echo $$ > pid\nsleep 30 &\necho $! > child\nwait");
    let dir = speaker.parent().unwrap().to_path_buf();
    let mut harness = harness(true);
    harness.state_mut().window.speaker = Some(speaker);
    viewing_summary(&mut harness);
    harness.run();
    harness
        .get_by_role_and_label(Role::Button, "Read aloud")
        .click();
    harness.run();
    let pids: Vec<String> = ["pid", "child"]
        .iter()
        .map(|name| {
            for _ in 0..200 {
                match std::fs::read_to_string(dir.join(name)) {
                    Ok(pid) if pid.ends_with('\n') => return pid.trim().to_owned(),
                    _ => std::thread::sleep(Duration::from_millis(10)),
                }
            }
            panic!("the fake never wrote {name}");
        })
        .collect();
    harness.get_by_role_and_label(Role::Button, "Stop").click();
    harness.run();
    assert!(harness.state().window.reading.is_none());
    harness.get_by_role_and_label(Role::Button, "Read aloud");
    for pid in pids {
        let proc = PathBuf::from(format!("/proc/{pid}"));
        for _ in 0..300 {
            // Gone, or a zombie its parent has not reaped yet.
            let state = std::fs::read_to_string(proc.join("stat")).unwrap_or_default();
            if state.is_empty() || state.contains(") Z ") {
                break;
            }
            std::thread::sleep(Duration::from_millis(10));
        }
        let state = std::fs::read_to_string(proc.join("stat")).unwrap_or_default();
        assert!(
            state.is_empty() || state.contains(") Z "),
            "{pid} still runs"
        );
    }
    assert!(harness.state().window.read_error.is_none());
}

#[test]
fn an_old_vinospeak_says_to_update_it() {
    let speaker = fake_speaker(
        "echo 'usage: vinospeak [-h] [--file FILE]' >&2\n\
             echo 'vinospeak: error: unrecognized arguments: --part summary' >&2\nexit 2",
    );
    let mut harness = harness(true);
    harness.state_mut().window.speaker = Some(speaker);
    viewing_summary(&mut harness);
    harness.run();
    harness
        .get_by_role_and_label(Role::Button, "Read aloud")
        .click();
    until(&mut harness, |under| under.window.reading.is_none());
    harness.get_by_label(
        "Reading aloud stopped: this vinoSpeak can't read sessions yet. Update vinoSpeak \
             to read them.",
    );
    harness
        .get_by_role_and_label(Role::Button, "Dismiss")
        .click();
    harness.run();
    assert!(harness.state().window.read_error.is_none());
}

#[test]
fn going_back_to_all_sessions_stops_it_and_a_card_reads_its_summary() {
    let speaker = fake_speaker("echo \"$@\" > args\nexec sleep 30");
    let args = speaker.with_file_name("args");
    let mut harness = harness(true);
    harness.state_mut().window.speaker = Some(speaker);
    viewing_summary(&mut harness);
    harness.run();
    harness
        .get_by_role_and_label(Role::Button, "Read aloud")
        .click();
    harness.run();
    assert!(harness.state().window.reading.is_some());
    harness
        .get_by_role_and_label(Role::Button, "All sessions")
        .click();
    harness.run();
    assert!(harness.state().window.reading.is_none());

    // The first card has a summary; its More menu reads that, then offers to stop.
    harness.state_mut().window.state.lock().unwrap().listing = Some(Ok(listing()));
    harness.run();
    let _ = std::fs::remove_file(&args);
    harness
        .query_all_by_role_and_label(Role::Button, "More")
        .next()
        .unwrap()
        .click();
    harness.run();
    harness.get_by_label("Read aloud").click();
    harness.run();
    until(&mut harness, |_| args.exists());
    assert_eq!(
        std::fs::read_to_string(&args).unwrap(),
        "--file /t/b.vwsession --part summary\n"
    );
    harness
        .query_all_by_role_and_label(Role::Button, "More")
        .next()
        .unwrap()
        .click();
    harness.run();
    harness.get_by_label("Stop reading").click();
    harness.run();
    assert!(harness.state().window.reading.is_none());
}

fn importer_answering(
    harness: &mut Harness<'static, Under>,
    events: Vec<ImportEvent>,
    answer: Result<Option<PathBuf>, String>,
) {
    harness.state_mut().window.importer = Arc::new(move |on_event| {
        for event in events.clone() {
            on_event(event);
        }
        answer.clone()
    });
}

fn importing(name: &str, position_s: f64, duration_s: Option<f64>) -> Import {
    Import {
        name: name.to_owned(),
        position_s,
        duration_s,
        ..Import::default()
    }
}

#[test]
fn an_import_that_lost_its_speech_detector_says_so_with_the_result() {
    let mut harness = harness(true);
    importer_answering(
        &mut harness,
        vec![
            ImportEvent::Chosen("lecture.mp3".to_owned()),
            ImportEvent::Warning("the speech detector stopped at 0:05".to_owned()),
        ],
        Ok(Some("/t/c.vwsession".into())),
    );
    harness.state_mut().window.state.lock().unwrap().listing = Some(Ok(listing()));
    harness.run();
    harness
        .get_by_role_and_label(Role::Button, "Import audio or video…")
        .click();
    until(&mut harness, |under| {
        under.window.state.lock().unwrap().imported.is_some()
    });
    harness.run();
    harness.get_by_label("Imported. It's in the list, and is summarised like any session.");
    harness.get_by_label("Import note: the speech detector stopped at 0:05.");
    // The next import starts with none.
    harness
        .get_by_role_and_label(Role::Button, "Import audio or video…")
        .click();
    until(&mut harness, |under| {
        under.window.state.lock().unwrap().imported.is_some()
    });
    assert_eq!(
        harness
            .state()
            .window
            .state
            .lock()
            .unwrap()
            .import_warnings
            .len(),
        1,
        "one per import, not one more each time"
    );
}

#[test]
fn import_audio_transcribes_the_file_picked_and_offers_its_transcript() {
    let mut harness = harness(true);
    importer_answering(
        &mut harness,
        vec![ImportEvent::Chosen("lecture.mp3".to_owned())],
        Ok(Some("/t/c.vwsession".into())),
    );
    harness.state_mut().window.state.lock().unwrap().listing = Some(Ok(listing()));
    harness.run();
    harness
        .get_by_role_and_label(Role::Button, "Import audio or video…")
        .click();
    until(&mut harness, |under| {
        under.window.state.lock().unwrap().imported.is_some()
    });
    harness.run();
    harness.get_by_label("Imported. It's in the list, and is summarised like any session.");
    // The list is read again by the window's next frame, not by this test.
    assert!(harness.state().window.state.lock().unwrap().relist);
    harness.get_by_role_and_label(Role::Button, "Transcript of the import");
    assert_eq!(
        harness.state().window.state.lock().unwrap().imported,
        Some(Ok(PathBuf::from("/t/c.vwsession")))
    );
}

#[test]
fn an_import_under_way_says_how_far_it_is_and_cannot_be_started_twice() {
    let mut harness = harness(true);
    {
        let mut state = harness.state_mut().window.state.lock().unwrap();
        state.listing = Some(Ok(listing()));
        state.import = Some(importing("lecture.mp3", 750.0, Some(2700.0)));
    }
    harness.run();
    harness.get_by_label("Transcribing lecture.mp3: 12:30 of 45:00.");
    let button = harness.get_by_role_and_label(Role::Button, "Import audio or video…");
    assert!(button.accesskit_node().is_disabled());

    // Without ffprobe the length is not known.
    harness.state_mut().window.state.lock().unwrap().import =
        Some(importing("lecture.mp3", 750.0, None));
    harness.run();
    harness.get_by_label("Transcribing lecture.mp3: 12:30 so far.");

    // Before the dialog answers there is no file to name.
    harness.state_mut().window.state.lock().unwrap().import = Some(Import::default());
    harness.run();
    harness.get_by_label("Waiting for the file dialog.");
}

#[test]
fn cancel_marks_the_import_and_its_ending_is_not_reported_as_a_failure() {
    let mut harness = harness(true);
    {
        let mut state = harness.state_mut().window.state.lock().unwrap();
        state.listing = Some(Ok(listing()));
        // No pid: nothing real to signal.
        state.import = Some(importing("lecture.mp3", 10.0, Some(60.0)));
    }
    harness.run();
    harness
        .get_by_role_and_label(Role::Button, "Cancel")
        .click();
    harness.run();
    harness.get_by_label("Stopping lecture.mp3.");
    assert!(
        harness
            .state()
            .window
            .state
            .lock()
            .unwrap()
            .import
            .as_ref()
            .unwrap()
            .cancelled
    );
}

#[test]
fn a_failed_import_says_why_and_a_closed_dialog_says_nothing() {
    let mut harness = harness(true);
    importer_answering(
        &mut harness,
        vec![ImportEvent::Chosen("quiet.wav".to_owned())],
        Err("no speech found in quiet.wav; nothing was saved".to_owned()),
    );
    harness.state_mut().window.state.lock().unwrap().listing = Some(Ok(listing()));
    harness.run();
    harness
        .get_by_role_and_label(Role::Button, "Import audio or video…")
        .click();
    until(&mut harness, |under| {
        under.window.state.lock().unwrap().imported.is_some()
    });
    harness.run();
    harness.get_by_label("Import didn't work: no speech found in quiet.wav; nothing was saved");
    assert!(!harness.state().window.state.lock().unwrap().relist);
    harness
        .get_by_role_and_label(Role::Button, "Dismiss")
        .click();
    harness.run();

    importer_answering(&mut harness, Vec::new(), Ok(None));
    harness
        .get_by_role_and_label(Role::Button, "Import audio or video…")
        .click();
    until(&mut harness, |under| {
        under.window.state.lock().unwrap().import.is_none()
    });
    harness.run();
    assert!(
        harness
            .query_by_label_contains("Import didn't work")
            .is_none()
    );
    assert!(harness.query_by_label_contains("Imported.").is_none());
}

#[test]
fn a_narrow_window_keeps_import_as_a_named_plus_button() {
    let mut harness = harness(true);
    harness.state_mut().window.state.lock().unwrap().listing = Some(Ok(listing()));
    harness.set_size(egui::vec2(340.0, 560.0));
    harness.run();
    // Same name for a screen reader; the text label would run under the title.
    harness.get_by_role_and_label(Role::Button, "Import audio or video…");
    assert!(harness.query_by_label("Import audio or video…").is_some());
    harness.get_by_role_and_label(Role::Heading, "Saved sessions");
}

fn opener_answering(
    harness: &mut Harness<'static, Under>,
    answer: Result<Option<Opened>, String>,
) -> Arc<Mutex<Vec<ExportRequest>>> {
    let seen = Arc::new(Mutex::new(Vec::new()));
    let recorded = Arc::clone(&seen);
    harness.state_mut().window.opener = Arc::new(move |request| {
        recorded.lock().unwrap().push(request.clone());
        answer.clone()
    });
    seen
}

fn until_opened(harness: &mut Harness<'static, Under>) {
    for _ in 0..500 {
        harness.step();
        if !harness.state().window.state.lock().unwrap().opening {
            harness.run();
            return;
        }
        std::thread::sleep(Duration::from_millis(5));
    }
    panic!("opening never finished");
}

fn opened(path: &str, state: &str) -> Opened {
    Opened {
        exported: Exported {
            path: path.into(),
            state: state.into(),
            ..Exported::default()
        },
        fallback: true,
    }
}

#[test]
fn open_in_sends_the_tab_being_read_to_the_last_app_picked() {
    let mut harness = harness(true);
    let seen = opener_answering(
        &mut harness,
        Ok(Some(opened(
            "/d/2026-10-27 Local captions talk.odt",
            "written",
        ))),
    );
    {
        let mut state = harness.state_mut().window.state.lock().unwrap();
        state.listing = Some(Ok(listing()));
        state.viewer = reading("summary", "# Local captions talk\n\nText.\n");
    }
    harness.run();
    harness.get_by_role_and_label(Role::Button, "Open in…");
    harness
        .get_by_role_and_label(Role::Button, "Open in word processor")
        .click();
    until_opened(&mut harness);
    assert_eq!(
        *seen.lock().unwrap(),
        [ExportRequest {
            path: "/t/b.vwsession".into(),
            scope: Scope::Summary,
            format: Format::Odt,
        }]
    );
    harness.get_by_label("Opened /d/2026-10-27 Local captions talk.odt.");
    // Using the remembered app writes nothing.
    assert!(!harness.state().window.settings_path.exists());
}

#[test]
fn the_app_picked_from_the_menu_is_remembered_in_the_settings() {
    let mut harness = harness(true);
    let seen = opener_answering(&mut harness, Ok(None));
    harness.state_mut().window.state.lock().unwrap().viewer =
        reading("transcript", "[00:00] One.\n");
    harness.run();
    harness
        .get_by_role_and_label(Role::Button, "Open in…")
        .click();
    harness.run();
    harness.get_by_label("Text editor").click();
    until_opened(&mut harness);
    assert_eq!(seen.lock().unwrap()[0].format, Format::Markdown);
    assert_eq!(seen.lock().unwrap()[0].scope, Scope::Transcript);
    // A closed app chooser says nothing.
    assert!(harness.query_by_label_contains("Opened").is_none());
    harness.get_by_role_and_label(Role::Button, "Open in text editor");

    let path = harness.state().window.settings_path.clone();
    assert_eq!(Settings::load_from(&path).open_in, OpenIn::TextEditor);
    // The next window starts on it.
    let mut next = SessionsWindow::with(true);
    next.open_in = Settings::load_from(&path).open_in;
    assert_eq!(next.open_in, OpenIn::TextEditor);
    let _ = std::fs::remove_dir_all(path.parent().unwrap());
}

#[test]
fn open_in_is_off_on_the_summary_tab_of_a_session_with_no_summary() {
    let mut harness = harness(true);
    {
        let mut state = harness.state_mut().window.state.lock().unwrap();
        state.listing = Some(Ok(listing()));
        state.viewer = Viewer {
            target: Some(Target::parse("/t/a.vwsession#summary")),
            text: Some(Err("a.vwsession has no summary yet".into())),
            ..Viewer::default()
        };
    }
    harness.run();
    let button = harness.get_by_role_and_label(Role::Button, "Open in word processor");
    assert!(button.accesskit_node().is_disabled());
    let menu = harness.get_by_role_and_label(Role::Button, "Open in…");
    assert!(menu.accesskit_node().is_disabled());

    harness.state_mut().window.state.lock().unwrap().viewer =
        reading("transcript", "[00:00] One.\n");
    harness.run();
    let button = harness.get_by_role_and_label(Role::Button, "Open in word processor");
    assert!(!button.accesskit_node().is_disabled());
}

#[test]
fn an_edited_copy_is_opened_as_it_is_and_the_notice_says_so() {
    let mut harness = harness(true);
    opener_answering(
        &mut harness,
        Ok(Some(opened("/d/2026-10-26 Transcript.md", "edited"))),
    );
    harness.state_mut().window.state.lock().unwrap().listing = Some(Ok(listing()));
    harness.run();
    // The card without a summary sends its transcript.
    harness
        .query_all_by_role_and_label(Role::Button, "More")
        .nth(1)
        .unwrap()
        .click();
    harness.run();
    harness.get_by_label("Open in word processor");
    harness.get_by_label("Open in text editor").click();
    until_opened(&mut harness);
    harness.get_by_label(
        "Opened /d/2026-10-26 Transcript.md as it is: it differs from the session (edited, \
             or summarised again), so it wasn't replaced. Export… writes a fresh copy.",
    );
}

#[test]
fn a_failure_to_open_says_why() {
    let mut harness = harness(true);
    opener_answering(
        &mut harness,
        Err("xdg-open found no app for /d/x.odt".into()),
    );
    harness.state_mut().window.state.lock().unwrap().viewer =
        reading("transcript", "[00:00] One.\n");
    harness.run();
    harness
        .get_by_role_and_label(Role::Button, "Open in word processor")
        .click();
    until_opened(&mut harness);
    harness.get_by_label("Couldn't open it: xdg-open found no app for /d/x.odt");
}
