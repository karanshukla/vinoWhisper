use super::*;
use eframe::egui::accesskit::{Live, Role, Toggled};
use egui_kittest::Harness;
use egui_kittest::kittest::{NodeT, Queryable};

// No file is read or written and nothing is spawned: `show` is the window minus its
// side effects, and the tests never press the buttons that run the tray or vinowhisper-cleanup.
struct Under {
    window: SettingsWindow,
    styled: bool,
    saved: (bool, bool),
}

fn harness(page: Page, width: f32) -> Harness<'static, Under> {
    let mut window = SettingsWindow::with(
        Some(page),
        Settings::default(),
        Speech::default(),
        Cleanup::default(),
    );
    window.multilingual = true;
    window.apps_data = std::env::temp_dir().join(format!(
        "vinowhisper-gui-test-{}-{:?}",
        std::process::id(),
        std::thread::current().id()
    ));
    window.model.lock().unwrap().status = Some(Ok(Status {
        label: "SmolLM3-3B".into(),
        download_bytes: 1_702_000_000,
        ..Status::default()
    }));
    *window.shortcuts.lock().unwrap() = Shortcuts::Bound {
        captions: "Meta+Alt+C".into(),
        dictate: "Meta+H".into(),
        configurable: true,
    };
    Harness::builder().with_size((width, 740.0)).build_ui_state(
        |ui, under: &mut Under| {
            let ctx = &ui.ctx().clone();
            // Fonts only bind at the next pass, and the bold family is not stock.
            if !under.styled {
                theme::apply(ctx);
                under.styled = true;
                ctx.request_repaint();
                return;
            }
            let (settings, speech) = under.window.show(ui);
            under.saved.0 |= settings;
            under.saved.1 |= speech;
        },
        Under {
            window,
            styled: false,
            saved: (false, false),
        },
    )
}

const WIDE: f32 = 820.0;
const NARROW: f32 = 400.0;

#[test]
fn the_app_s_title_bar_has_the_window_buttons_and_the_title_once() {
    for width in [WIDE, NARROW] {
        for app_title_bar in [true, false] {
            let mut harness = harness(Page::Captions, width);
            harness.state_mut().window.app_title_bar = app_title_bar;
            harness.run();
            for name in ["Minimise", "Maximise", "Close"] {
                assert_eq!(
                    harness
                        .query_by_role_and_label(Role::Button, name)
                        .is_some(),
                    app_title_bar,
                    "{name} at {width}, app_title_bar {app_title_bar}"
                );
            }
            // In the bar when wide, in the scroll area otherwise: never both.
            assert_eq!(
                harness
                    .query_all_by_role_and_label(Role::Heading, "Captions")
                    .count(),
                1,
                "at {width}, app_title_bar {app_title_bar}"
            );
        }
    }
}

#[test]
fn every_page_is_a_named_tab_and_only_the_current_one_is_selected() {
    for width in [WIDE, NARROW] {
        let mut harness = harness(Page::Captions, width);
        harness.run();
        for page in Page::ALL {
            let name = tab_name(page);
            let tab = harness.get_by_role_and_label(Role::Tab, &name);
            assert_eq!(
                tab.accesskit_node().is_selected(),
                Some(page == Page::Captions),
                "{} at {width}",
                page.title()
            );
        }
    }
}

#[test]
fn a_tab_opens_its_page_by_pointer_and_by_keyboard() {
    let mut harness = harness(Page::Shortcuts, WIDE);
    harness.run();
    harness.get_by_role_and_label(Role::Tab, "Speech").click();
    harness.run();
    assert!(harness.state().window.page == Page::Speech);

    harness.get_by_role_and_label(Role::Tab, "Captions").focus();
    harness.run();
    assert!(
        harness
            .get_by_role_and_label(Role::Tab, "Captions")
            .is_focused()
    );
    harness.key_press(egui::Key::Enter);
    harness.run();
    assert!(harness.state().window.page == Page::Captions);
}

#[test]
fn tab_moves_focus_from_one_page_tab_to_the_next() {
    let mut harness = harness(Page::Shortcuts, WIDE);
    harness.run();
    harness
        .get_by_role_and_label(Role::Tab, "Shortcuts")
        .focus();
    harness.run();
    harness.key_press(egui::Key::Tab);
    harness.run();
    assert!(
        harness
            .get_by_role_and_label(Role::Tab, "Captions")
            .is_focused()
    );
    harness.key_press(egui::Key::Space);
    harness.run();
    assert!(harness.state().window.page == Page::Captions);
}

#[test]
fn the_captions_page_names_every_control_after_its_label() {
    for width in [WIDE, NARROW] {
        let mut harness = harness(Page::Captions, width);
        harness.run();
        for label in ["Position", "Text size", "Listen to"] {
            harness.get_by_role_and_label(Role::RadioGroup, label);
        }
        for label in ["Hide after silence", "Hide icon after"] {
            harness.get_by_role_and_label(Role::SpinButton, label);
        }
        harness.get_by_role_and_label(Role::Switch, "Save transcripts");
        for title in ["Overlay", "Listening", "Tray"] {
            harness.get_by_role_and_label(Role::Heading, title);
        }
    }
}

#[test]
fn a_segmented_control_can_be_changed_and_says_what_it_is_set_to() {
    let mut harness = harness(Page::Captions, WIDE);
    harness.run();
    let bottom = harness.get_by_role_and_label(Role::RadioButton, "Bottom");
    assert_eq!(bottom.accesskit_node().toggled(), Some(Toggled::True));
    harness
        .get_by_role_and_label(Role::RadioButton, "Top")
        .click();
    harness.run();
    assert_eq!(harness.state().window.settings.position, Position::Top);
    assert!(harness.state().saved.0);
    let top = harness.get_by_role_and_label(Role::RadioButton, "Top");
    assert_eq!(top.accesskit_node().toggled(), Some(Toggled::True));
}

#[test]
fn escape_closes_an_open_combo_box_without_changing_it() {
    let mut harness = harness(Page::Cleanup, WIDE);
    harness.state_mut().window.cleanup.enabled = true;
    harness.run();
    harness.get_by_role_and_label(Role::ComboBox, "Run").click();
    harness.run();
    assert!(harness.query_by_label("Every dictation").is_some());
    harness.key_press(egui::Key::Escape);
    harness.run();
    assert!(harness.query_by_label("Every dictation").is_none());
    assert_eq!(harness.state().window.cleanup.mode, CleanupMode::Smart);
}

#[test]
fn a_switch_reports_and_flips_its_state() {
    let mut harness = harness(Page::Captions, WIDE);
    harness.run();
    let switch = harness.get_by_role_and_label(Role::Switch, "Save transcripts");
    let before = switch.accesskit_node().toggled();
    switch.focus();
    harness.run();
    harness.key_press(egui::Key::Space);
    harness.run();
    assert!(harness.state().window.settings.save_transcripts);
    assert!(harness.state().saved.0);
    let after = harness
        .get_by_role_and_label(Role::Switch, "Save transcripts")
        .accesskit_node()
        .toggled();
    assert_ne!(before, after);
}

#[test]
fn the_speech_page_names_its_controls() {
    let mut harness = harness(Page::Speech, WIDE);
    harness.run();
    harness.get_by_role_and_label(Role::ComboBox, "Speech model");
    harness.get_by_role_and_label(Role::ComboBox, "Language");
    harness.get_by_role_and_label(Role::Switch, "Translate to English");
    harness.get_by_role_and_label(Role::Heading, "Dictation");
}

#[test]
fn pausing_media_is_a_switch_that_is_off_and_marks_the_settings_to_save() {
    let mut harness = harness(Page::Speech, WIDE);
    harness.run();
    let switch = harness.get_by_role_and_label(Role::Switch, "Pause media");
    assert_eq!(switch.accesskit_node().toggled(), Some(Toggled::False));
    switch.click();
    harness.run();
    assert!(harness.state().window.settings.pause_media);
}

#[test]
fn spacing_between_dictations_is_a_switch_that_is_off_until_asked_for() {
    let mut harness = harness(Page::Speech, WIDE);
    harness.run();
    let switch = harness.get_by_role_and_label(Role::Switch, "Space between dictations");
    assert_eq!(switch.accesskit_node().toggled(), Some(Toggled::False));
    switch.click();
    harness.run();
    assert!(harness.state().window.settings.smart_spacing);
}

fn vocabulary_harness(name: &str) -> Harness<'static, Under> {
    let mut harness = harness(Page::Speech, WIDE);
    // The vocabulary is the last row on the page: tall enough that it is not scrolled away.
    harness.set_size(egui::vec2(WIDE, 1000.0));
    let path = harness.state().window.apps_data.join(name);
    let _ = std::fs::remove_file(&path);
    harness.state_mut().window.vocabulary_path = path;
    harness.run();
    harness
}

#[test]
fn a_term_typed_into_the_vocabulary_is_written_to_the_file_and_listed() {
    let mut harness = vocabulary_harness("vocabulary-add.txt");
    assert!(
        harness
            .query_by_role_and_label(Role::Button, "Add")
            .is_some_and(|add| add.accesskit_node().is_disabled())
    );
    harness
        .get_by_role_and_label(Role::TextInput, "New term")
        .focus();
    harness.run();
    harness
        .get_by_role_and_label(Role::TextInput, "New term")
        .type_text("Kubernetes");
    harness.run();
    assert_eq!(harness.state().window.new_term, "Kubernetes");
    harness.get_by_role_and_label(Role::Button, "Add").click();
    harness.run();
    let path = harness.state().window.vocabulary_path.clone();
    assert_eq!(std::fs::read_to_string(&path).unwrap(), "Kubernetes\n");
    harness.get_by_role_and_label(Role::Button, "Remove Kubernetes");
    assert!(harness.state().window.new_term.is_empty());
}

#[test]
fn removing_a_term_takes_it_out_of_the_file() {
    let mut harness = vocabulary_harness("vocabulary-remove.txt");
    let path = harness.state().window.vocabulary_path.clone();
    std::fs::create_dir_all(path.parent().unwrap()).unwrap();
    std::fs::write(&path, "Kubernetes\nSaoirse\n").unwrap();
    harness.state_mut().window.vocabulary = vocabulary::load_from(&path);
    harness.run();
    harness
        .get_by_role_and_label(Role::Button, "Remove Kubernetes")
        .click();
    harness.run();
    assert_eq!(std::fs::read_to_string(&path).unwrap(), "Saoirse\n");
    assert!(
        harness
            .query_by_role_and_label(Role::Button, "Remove Kubernetes")
            .is_none()
    );
}

#[test]
fn canary_says_how_to_install_it_until_it_is_there() {
    let mut harness = harness(Page::Speech, WIDE);
    harness.run();
    harness.get_by_label_contains("Whisper small, 0.9 GB");
    harness.state_mut().window.speech.model = SpeechModel::Canary;
    harness.run();
    harness.get_by_label_contains("not downloaded yet: use Download below");
    harness.state_mut().window.canary = true;
    harness.run();
    harness.get_by_label_contains("25 European languages");
    assert!(
        harness
            .query_by_label_contains("not downloaded yet")
            .is_none()
    );
}

#[test]
fn canary_offers_its_own_languages_and_detection_to_translate() {
    let mut harness = harness(Page::Speech, WIDE);
    harness.state_mut().window.canary = true;
    harness.state_mut().window.speech.model = SpeechModel::Canary;
    harness.run();
    harness
        .get_by_role_and_label(Role::ComboBox, "Language")
        .click();
    harness.run();
    harness.get_by_label("Italian");
    harness.get_by_label("Detect automatically");
}

#[test]
fn switching_to_canary_keeps_auto_translate() {
    let mut harness = harness(Page::Speech, WIDE);
    let window = &mut harness.state_mut().window;
    window.canary = true;
    window.speech.language = Language::Auto;
    window.speech.task = Task::Translate;
    harness.run();
    harness
        .get_by_role_and_label(Role::ComboBox, "Speech model")
        .click();
    harness.run();
    harness.get_by_label("Canary 1B v2").click();
    harness.run();
    let speech = harness.state().window.speech;
    assert_eq!(
        (speech.model, speech.language, speech.task),
        (SpeechModel::Canary, Language::Auto, Task::Translate)
    );
    assert!(harness.state().window.speech_note.is_none());
    let switch = harness.get_by_role_and_label(Role::Switch, "Translate to English");
    assert!(
        switch.accesskit_node().is_disabled(),
        "locked on for Canary's auto"
    );
    assert_eq!(
        switch.accesskit_node().toggled(),
        Some(egui::accesskit::Toggled::True)
    );
}

#[test]
fn a_model_switch_that_drops_the_language_says_so() {
    let mut harness = harness(Page::Speech, WIDE);
    let window = &mut harness.state_mut().window;
    window.canary = true;
    window.speech.language = Language::Auto;
    harness.run();
    harness
        .get_by_role_and_label(Role::ComboBox, "Speech model")
        .click();
    harness.run();
    harness.get_by_label("Canary 1B v2").click();
    harness.run();
    assert_eq!(harness.state().window.speech.language, Language::En);
    harness.get_by_label_contains("so Language is now English");
}

fn canary_status(present: bool) -> Status {
    Status {
        label: "Canary 1B v2".into(),
        download_bytes: 972_131_242,
        present,
        bytes: if present { 972_131_242 } else { 0 },
        cache_bytes: 0,
        source_bytes: 0,
    }
}

#[test]
fn canary_can_be_downloaded_from_the_speech_page() {
    let mut harness = harness(Page::Speech, WIDE);
    harness
        .state_mut()
        .window
        .canary_model
        .lock()
        .unwrap()
        .status = Some(Ok(canary_status(false)));
    harness.run();
    harness.get_by_label("Not downloaded (972 MB)");
    harness.get_by_role_and_label(Role::Button, "Download");
    harness
        .get_by_role_and_label(Role::ComboBox, "Speech model")
        .click();
    harness.run();
    harness.get_by_label("Canary 1B v2 (not installed)");
}

#[test]
fn a_downloaded_canary_can_be_picked() {
    let mut harness = harness(Page::Speech, WIDE);
    harness
        .state_mut()
        .window
        .canary_model
        .lock()
        .unwrap()
        .status = Some(Ok(canary_status(true)));
    harness.run();
    harness.get_by_label("Downloaded, 972 MB");
    harness
        .get_by_role_and_label(Role::ComboBox, "Speech model")
        .click();
    harness.run();
    // The download group is headed "Canary 1B v2" too; the option is the one that isn't a label.
    harness
        .get_all_by_label("Canary 1B v2")
        .find(|node| node.accesskit_node().role() != Role::Label)
        .unwrap()
        .click();
    harness.run();
    assert_eq!(harness.state().window.speech.model, SpeechModel::Canary);
    assert!(harness.state().saved.1, "the switch is saved");
}

#[test]
fn deleting_the_canary_in_use_goes_back_to_whisper() {
    let mut window = SettingsWindow::with(
        Some(Page::Speech),
        Settings::default(),
        Speech {
            language: Language::Auto,
            task: Task::Translate,
            model: SpeechModel::Canary,
        },
        Cleanup::default(),
    );
    assert!(window.forget_canary());
    assert_eq!(
        window.speech,
        Speech {
            language: Language::Auto,
            task: Task::Translate,
            model: SpeechModel::Whisper,
        }
    );
    assert!(window.speech_note.is_some());
    assert!(!window.forget_canary(), "Whisper in use: nothing to switch");
}

fn whisper_status(present: bool, source_bytes: u64) -> Status {
    Status {
        label: "Whisper small".into(),
        present,
        bytes: if present { 930_000_000 } else { 0 },
        source_bytes,
        ..Status::default()
    }
}

#[test]
fn whisper_in_use_confirms_its_delete_in_place_and_says_what_stops() {
    let mut harness = harness(Page::Speech, WIDE);
    harness
        .state_mut()
        .window
        .whisper_model
        .lock()
        .unwrap()
        .status = Some(Ok(whisper_status(true, 967_000_000)));
    harness.run();
    harness.get_by_label("On disk, 930 MB");
    harness.get_by_label("967 MB");
    harness
        .get_all_by_role_and_label(Role::Button, "Delete…")
        .next()
        .unwrap()
        .click();
    harness.run();
    assert!(harness.state().window.confirm_delete_whisper);
    harness.get_by_label_contains("Deletes every Whisper export and its source download (1.9 GB)");
    harness.get_by_label_contains("stay off until you run vinowhisper-setup again");
    harness.get_by_role_and_label(Role::Button, "Delete Whisper");
    harness
        .get_by_role_and_label(Role::Button, "Cancel")
        .click();
    harness.run();
    assert!(!harness.state().window.confirm_delete_whisper);
}

#[test]
fn deleting_whisper_under_canary_says_canary_keeps_running() {
    let mut harness = harness(Page::Speech, WIDE);
    {
        let window = &mut harness.state_mut().window;
        window.canary = true;
        window.speech.model = SpeechModel::Canary;
        window.confirm_delete_whisper = true;
        window.whisper_model.lock().unwrap().status = Some(Ok(whisper_status(true, 0)));
    }
    harness.run();
    harness.get_by_label_contains("Canary keeps running");
    assert!(
        harness.query_by_label("Source download").is_none(),
        "no source on disk, no row"
    );
}

#[test]
fn the_source_download_has_its_own_safe_delete() {
    let mut harness = harness(Page::Speech, WIDE);
    {
        let window = &mut harness.state_mut().window;
        window.confirm_delete_source = true;
        window.whisper_model.lock().unwrap().status = Some(Ok(whisper_status(true, 967_000_000)));
    }
    harness.run();
    harness.get_by_label_contains("Captions and dictation keep working");
    harness.get_by_role_and_label(Role::Button, "Delete the download");
    assert!(!harness.state().window.confirm_delete_whisper);
}

#[test]
fn a_deleted_whisper_is_not_installed_and_says_how_to_get_it_back() {
    let mut harness = harness(Page::Speech, WIDE);
    harness
        .state_mut()
        .window
        .whisper_model
        .lock()
        .unwrap()
        .status = Some(Ok(whisper_status(false, 0)));
    harness.run();
    harness.get_by_label("Not exported");
    harness.get_by_label_contains("Run vinowhisper-setup to export it again");
    let delete = harness.get_by_role_and_label(Role::Button, "Delete…");
    assert!(delete.accesskit_node().is_disabled(), "nothing to delete");
    harness
        .get_by_role_and_label(Role::ComboBox, "Speech model")
        .click();
    harness.run();
    harness.get_by_label("Whisper small (not installed)");
}

#[test]
fn a_whisper_delete_that_failed_changes_nothing_on_the_page() {
    let mut harness = harness(Page::Speech, WIDE);
    {
        let window = &mut harness.state_mut().window;
        window.multilingual = true;
        window.whisper_delete_pending = true;
        let mut state = window.whisper_model.lock().unwrap();
        state.busy = true;
        state.status = Some(Ok(whisper_status(true, 0)));
    }
    harness.run();
    assert!(
        harness.state().window.whisper_delete_pending,
        "still deleting: nothing is decided yet"
    );
    {
        let window = &mut harness.state_mut().window;
        let mut state = window.whisper_model.lock().unwrap();
        state.busy = false;
        state.error = "Permission denied".into();
    }
    harness.run();
    let window = &harness.state().window;
    assert!(!window.whisper_delete_pending);
    assert!(window.multilingual, "the exports are still there");
    assert!(window.speech_note.is_none(), "nothing was deleted");
    harness.get_by_label_contains("The last attempt failed: Permission denied");
}

#[test]
fn a_whisper_delete_takes_effect_once_the_status_says_it_is_gone() {
    let mut harness = harness(Page::Speech, WIDE);
    {
        let window = &mut harness.state_mut().window;
        window.multilingual = true;
        window.whisper_delete_pending = true;
        window.whisper_model.lock().unwrap().status = Some(Ok(whisper_status(false, 0)));
    }
    harness.run();
    let window = &harness.state().window;
    assert!(!window.whisper_delete_pending && !window.multilingual);
    assert!(
        window
            .speech_note
            .as_deref()
            .unwrap()
            .contains("Whisper was deleted")
    );
}

#[test]
fn a_whisper_status_that_could_not_be_read_is_shown() {
    let mut harness = harness(Page::Speech, WIDE);
    harness
        .state_mut()
        .window
        .whisper_model
        .lock()
        .unwrap()
        .status = Some(Err("could not run vinowhisper-whisper: not found".into()));
    harness.run();
    harness.get_by_label_contains(
        "Could not check what is on disk: could not run vinowhisper-whisper: not found",
    );
}

#[test]
fn deleting_canary_with_no_whisper_export_says_everything_stays_off() {
    let mut harness = harness(Page::Speech, WIDE);
    {
        let window = &mut harness.state_mut().window;
        window.canary = true;
        window.speech.model = SpeechModel::Canary;
        window.confirm_delete_canary = true;
        window.canary_model.lock().unwrap().status = Some(Ok(canary_status(true)));
        window.whisper_model.lock().unwrap().status = Some(Ok(whisper_status(false, 0)));
    }
    harness.run();
    harness.get_by_label_contains("stay off until vinowhisper-setup exports a model");
    assert!(
        harness
            .query_by_label_contains("Whisper takes over")
            .is_none()
    );
    let window = &mut harness.state_mut().window;
    assert!(window.forget_canary());
    assert!(
        window
            .speech_note
            .as_deref()
            .unwrap()
            .contains("Run vinowhisper-setup to export a model")
    );
}

#[test]
fn a_canary_delete_that_failed_leaves_canary_the_speech_model() {
    let mut harness = harness(Page::Speech, WIDE);
    {
        let window = &mut harness.state_mut().window;
        window.canary = true;
        window.speech.model = SpeechModel::Canary;
        window.canary_delete_pending = true;
        let mut state = window.canary_model.lock().unwrap();
        state.busy = true;
        state.status = Some(Ok(canary_status(true)));
    }
    harness.run();
    assert!(
        harness.state().window.canary_delete_pending,
        "still deleting: nothing is decided yet"
    );
    assert_eq!(harness.state().window.speech.model, SpeechModel::Canary);
    {
        let window = &mut harness.state_mut().window;
        let mut state = window.canary_model.lock().unwrap();
        state.busy = false;
        state.error = "Permission denied".into();
    }
    harness.run();
    let window = &harness.state().window;
    assert!(!window.canary_delete_pending);
    assert_eq!(window.speech.model, SpeechModel::Canary);
    assert!(window.speech_note.is_none(), "nothing was deleted");
}

#[test]
fn a_canary_delete_switches_to_whisper_once_the_status_says_it_is_gone() {
    let mut harness = harness(Page::Speech, WIDE);
    {
        let window = &mut harness.state_mut().window;
        window.canary = true;
        window.speech.model = SpeechModel::Canary;
        window.canary_delete_pending = true;
        window.canary_model.lock().unwrap().status = Some(Ok(canary_status(false)));
    }
    harness.run();
    let window = &harness.state().window;
    assert!(!window.canary_delete_pending);
    assert_eq!(window.speech.model, SpeechModel::Whisper);
    assert!(
        window
            .speech_note
            .as_deref()
            .unwrap()
            .contains("Canary was deleted")
    );
}

#[test]
fn forgetting_whisper_in_use_leaves_a_note() {
    let mut window = SettingsWindow::with(
        Some(Page::Speech),
        Settings::default(),
        Speech::default(),
        Cleanup::default(),
    );
    window.multilingual = true;
    window.forget_whisper();
    assert!(!window.multilingual);
    assert!(
        window
            .speech_note
            .as_deref()
            .unwrap()
            .contains("vinowhisper-setup")
    );
    window.speech_note = None;
    window.speech.model = SpeechModel::Canary;
    window.forget_whisper();
    assert!(
        window.speech_note.is_none(),
        "Canary in use: nothing stopped"
    );
}

#[test]
fn the_cleanup_delete_says_what_goes() {
    let mut harness = harness(Page::Cleanup, WIDE);
    {
        let window = &mut harness.state_mut().window;
        window.confirm_delete = true;
        window.model.lock().unwrap().status = Some(Ok(Status {
            label: "SmolLM3-3B".into(),
            present: true,
            bytes: 1_701_000_000,
            cache_bytes: 300_000_000,
            ..Status::default()
        }));
    }
    harness.run();
    harness.get_by_label_contains("Deletes the model and its compiled cache (2.0 GB)");
}

#[test]
fn translation_says_why_it_is_off_for_english() {
    let mut harness = harness(Page::Speech, WIDE);
    harness.run();
    let switch = harness.get_by_role_and_label(Role::Switch, "Translate to English");
    assert!(switch.accesskit_node().is_disabled());
    harness.get_by_label_contains("set Language to the one being spoken");
    harness.state_mut().window.speech.language = Language::Fr;
    harness.run();
    let switch = harness.get_by_role_and_label(Role::Switch, "Translate to English");
    assert!(!switch.accesskit_node().is_disabled());
    harness.get_by_label("Captions arrive in English whatever was spoken.");
}

#[test]
fn the_cleanup_page_names_its_fields_and_asks_for_no_key() {
    let mut harness = harness(Page::Cleanup, WIDE);
    harness.state_mut().window.cleanup.enabled = true;
    harness.run();
    harness.get_by_role_and_label(Role::Switch, "Clean up dictation");
    harness.get_by_role_and_label(Role::ComboBox, "Run");
    harness.get_by_role_and_label(Role::SpinButton, "Time limit");
    for gone in ["Address", "Model"] {
        assert!(
            harness
                .query_by_role_and_label(Role::TextInput, gone)
                .is_none(),
            "{gone}"
        );
    }
    assert!(
        harness
            .query_by_role_and_label(Role::PasswordInput, "New key")
            .is_none()
    );
    harness.get_by_label("Not downloaded (1.7 GB)");
    harness.get_by_label("Download the model below first; until then the raw text is typed.");
    let delete = harness.get_by_role_and_label(Role::Button, "Delete…");
    assert!(delete.accesskit_node().is_disabled(), "nothing to delete");
    harness.get_by_role_and_label(Role::Button, "Download");
    harness.get_by_role_and_label(Role::Heading, "Cleanup");
}

#[test]
fn a_downloaded_model_offers_verify_and_a_confirmed_delete() {
    let mut harness = harness(Page::Cleanup, WIDE);
    harness.state_mut().window.model.lock().unwrap().status = Some(Ok(Status {
        label: "SmolLM3-3B".into(),
        download_bytes: 1_702_000_000,
        present: true,
        bytes: 1_701_000_000,
        cache_bytes: 2_100_000_000,
        source_bytes: 0,
    }));
    harness.run();
    harness.get_by_label("Downloaded, 1.7 GB");
    harness.get_by_label("2.1 GB");
    harness.get_by_role_and_label(Role::Button, "Verify");
    harness
        .get_by_role_and_label(Role::Button, "Delete…")
        .click();
    harness.run();
    assert!(harness.state().window.confirm_delete);
    harness.get_by_role_and_label(Role::Button, "Delete the model");
    harness
        .get_by_role_and_label(Role::Button, "Cancel")
        .click();
    harness.run();
    assert!(!harness.state().window.confirm_delete);
}

#[test]
fn a_download_in_progress_shows_how_far_it_has_got() {
    let mut harness = harness(Page::Cleanup, WIDE);
    {
        let state = &mut harness.state_mut().window.model;
        let mut state = state.lock().unwrap();
        state.busy = true;
        state.fetching = Some((400_000_000, 1_702_000_000));
    }
    harness.run();
    harness.get_by_label("400 MB of 1.7 GB");
    let download = harness.get_by_role_and_label(Role::Button, "Download");
    assert!(
        download.accesskit_node().is_disabled(),
        "one fetch at a time"
    );
}

#[test]
fn a_failed_fetch_says_why() {
    let mut harness = harness(Page::Cleanup, WIDE);
    harness.state_mut().window.model.lock().unwrap().error = "sha256 mismatch".into();
    harness.run();
    harness.get_by_label("The last attempt failed: sha256 mismatch");
}

// AccessKit had the disabled flag right all along; accesskit_atspi_common before 0.21
// dropped it for buttons and told Orca every one was usable (#100, AccessKit#788). The
// tree above can't see that, so hold the lock at the fixed release.
#[test]
fn the_atspi_bridge_is_one_that_exposes_disabled_buttons() {
    let lock = include_str!("../../Cargo.lock");
    let version = lock
        .split("[[package]]")
        .find(|package| package.contains("name = \"accesskit_atspi_common\""))
        .and_then(|package| package.split("version = \"").nth(1))
        .and_then(|rest| rest.split('"').next())
        .expect("accesskit_atspi_common is locked");
    let minor: u32 = version.split('.').nth(1).unwrap().parse().unwrap();
    assert!(
        version.starts_with("0.") && minor >= 21,
        "accesskit_atspi_common {version} announces disabled buttons as usable"
    );
}

#[test]
fn the_model_status_is_announced_when_it_changes() {
    let mut harness = harness(Page::Cleanup, WIDE);
    harness.run();
    let status = harness.get_by_label("Not downloaded (1.7 GB)");
    assert_eq!(status.accesskit_node().live(), Live::Polite);
}

#[test]
fn fields_left_unused_by_cleanup_being_off_say_so() {
    let mut harness = harness(Page::Cleanup, WIDE);
    harness.run();
    assert!(!harness.state().window.cleanup.enabled);
    let limit = harness.get_by_role_and_label(Role::SpinButton, "Time limit");
    assert!(limit.accesskit_node().is_disabled());
}

#[test]
fn the_shortcuts_page_reads_the_keys_and_offers_change() {
    let mut harness = harness(Page::Shortcuts, WIDE);
    harness.run();
    harness.get_by_label("Meta+Alt+C");
    harness.get_by_label("Meta+H");
    harness.get_by_role_and_label(Role::Button, "Change…");
    harness.get_by_role_and_label(Role::Heading, "Shortcuts");
    harness.get_by_label("Running in the tray");
}

#[test]
fn nothing_focusable_is_nameless() {
    for page in Page::ALL {
        for width in [WIDE, NARROW] {
            let mut harness = harness(page, width);
            harness.state_mut().window.cleanup.enabled = true;
            harness.run();
            let nameless: Vec<String> = harness
                .query_all_by(|node| {
                    node.data().supports_action(egui::accesskit::Action::Focus)
                        && node.label().is_none_or(|label| label.trim().is_empty())
                })
                .map(|node| format!("{:?} at {:?}", node.accesskit_node().role(), node.rect()))
                .collect();
            assert!(
                nameless.is_empty(),
                "{} at {width}: {nameless:?}",
                page.title()
            );
        }
    }
}

#[test]
fn the_window_is_named() {
    let mut harness = harness(Page::Captions, WIDE);
    harness.run();
    assert_eq!(
        harness.root().accesskit_node().label().as_deref(),
        Some(TITLE)
    );
}

#[test]
fn the_sessions_page_keeps_the_setting_and_opens_the_sessions_window() {
    let mut harness = harness(Page::Sessions, WIDE);
    *harness.state_mut().window.model_ready.lock().unwrap() = Some(false);
    harness.run();
    harness.get_by_role_and_label(Role::Switch, "Summarise saved sessions automatically");
    harness.get_by_label("The model isn't downloaded yet; get it on the Cleanup page.");
    harness.get_by_role_and_label(Role::Button, "Go to Cleanup");
    harness.get_by_role_and_label(Role::Button, "Open saved sessions");
    harness.get_by_label("Saving transcripts is off (Captions page), so new sessions aren't kept.");
}

#[test]
fn the_app_menu_switch_adds_and_removes_the_sessions_entry() {
    let mut harness = harness(Page::Sessions, WIDE);
    let data = harness.state().window.apps_data.clone();
    let _ = std::fs::remove_dir_all(&data);
    harness.run();
    let switch = "Show saved sessions in the app menu";
    assert_eq!(
        harness
            .get_by_role_and_label(Role::Switch, switch)
            .accesskit_node()
            .toggled(),
        Some(egui::accesskit::Toggled::False),
        "off until turned on"
    );
    harness.get_by_role_and_label(Role::Switch, switch).click();
    harness.run();
    assert!(vinowhisper_settings::sessions_entry_shown_in(&data));
    let entry = std::fs::read_to_string(vinowhisper_settings::sessions_entry_in(&data)).unwrap();
    assert!(entry.contains("vinowhisper-gui --sessions\n"));
    harness.get_by_role_and_label(Role::Switch, switch).click();
    harness.run();
    assert!(!vinowhisper_settings::sessions_entry_shown_in(&data));
    let _ = std::fs::remove_dir_all(&data);
}

#[test]
fn a_full_screen_window_puts_a_page_s_groups_in_two_columns() {
    for (page, left, right) in [
        (Page::Captions, "Overlay", "Listening"),
        (Page::Speech, "Recognition", "Dictation"),
        (Page::Cleanup, "Dictation cleanup", "SmolLM3-3B"),
        (Page::Sessions, "Summaries", "Saved sessions"),
    ] {
        let mut harness = harness(page, 1440.0);
        harness.run();
        let left = harness.get_by_role_and_label(Role::Heading, left).rect();
        let right = harness.get_by_role_and_label(Role::Heading, right).rect();
        assert_eq!(left.top(), right.top(), "{}", page.title());
        assert!(right.left() > left.right(), "{}", page.title());
    }
}

#[test]
fn large_text_keeps_every_control_inside_the_window() {
    // 1.5 is the step where 820 wide first folds the sidebar; 3.0 is the clamp.
    for zoom in [1.5, *theme::TEXT_SCALE.end()] {
        for page in Page::ALL {
            let mut harness = harness(page, WIDE);
            harness.state_mut().window.cleanup.enabled = true;
            harness.ctx.set_zoom_factor(zoom);
            harness.run();
            let outside: Vec<String> = harness
                .query_all_by(|node| node.data().supports_action(egui::accesskit::Action::Focus))
                .filter(|node| node.rect().max.x > WIDE + 0.5 || node.rect().min.x < -0.5)
                .map(|node| format!("{:?} {:?}", node.accesskit_node().label(), node.rect()))
                .collect();
            assert!(
                outside.is_empty(),
                "{} at {zoom}x: {outside:?}",
                page.title()
            );
        }
    }
}

#[test]
fn the_desktop_text_scale_sets_zoom_but_keeps_a_later_ctrl_plus() {
    let mut harness = harness(Page::Captions, WIDE);
    harness.run();
    let ctx = harness.ctx.clone();
    // Zoom lands at the start of the next pass.
    let follow = |harness: &mut Harness<'static, Under>, scale| {
        harness.state_mut().window.follow_text_scale(&ctx, scale);
        harness.run();
        ctx.zoom_factor()
    };
    assert_eq!(follow(&mut harness, Some(1.25)), 1.25);
    harness.ctx.set_zoom_factor(1.5);
    harness.run();
    assert_eq!(
        follow(&mut harness, Some(1.25)),
        1.5,
        "unchanged desktop setting, user zoom kept"
    );
    assert_eq!(
        follow(&mut harness, None),
        1.0,
        "the desktop stopped asking"
    );
}

#[test]
fn doubled_text_size_folds_the_sidebar_and_keeps_every_control() {
    let mut harness = harness(Page::Captions, WIDE);
    harness.ctx.set_zoom_factor(2.0);
    harness.run();
    assert!(harness.ctx.content_rect().width() < widgets::SIDEBAR_FROM);
    harness.get_by_role_and_label(Role::Tab, "Cleanup (beta)");
    harness.get_by_role_and_label(Role::RadioGroup, "Position");
    harness.get_by_role_and_label(Role::Switch, "Save transcripts");
}
