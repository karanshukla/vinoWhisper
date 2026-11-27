use super::*;

pub(super) fn source_label(source: Source) -> &'static str {
    match source {
        Source::Output => "Playing",
        Source::Mic => "Microphone",
        Source::Both => "Both",
    }
}

pub(super) fn echo_label(mode: EchoCancel) -> &'static str {
    match mode {
        EchoCancel::Auto => "Auto",
        EchoCancel::On => "On",
        EchoCancel::Off => "Off",
    }
}

pub(super) fn position_label(position: Position) -> &'static str {
    match position {
        Position::Bottom => "Bottom",
        Position::Top => "Top",
    }
}

pub(super) fn size_label(size: TextSize) -> &'static str {
    match size {
        TextSize::Small => "Small",
        TextSize::Medium => "Medium",
        TextSize::Large => "Large",
    }
}

pub(super) fn mode_label(mode: CleanupMode) -> &'static str {
    match mode {
        CleanupMode::Smart => "Smart (only when needed)",
        CleanupMode::Always => "Every dictation",
    }
}

pub(super) fn language_label(language: Language) -> &'static str {
    match language {
        Language::En => "English",
        Language::Fr => "French",
        Language::De => "German",
        Language::Es => "Spanish",
        Language::Auto => "Detect automatically",
        Language::Bg => "Bulgarian",
        Language::Hr => "Croatian",
        Language::Cs => "Czech",
        Language::Da => "Danish",
        Language::Nl => "Dutch",
        Language::Et => "Estonian",
        Language::Fi => "Finnish",
        Language::El => "Greek",
        Language::Hu => "Hungarian",
        Language::It => "Italian",
        Language::Lv => "Latvian",
        Language::Lt => "Lithuanian",
        Language::Mt => "Maltese",
        Language::Pl => "Polish",
        Language::Pt => "Portuguese",
        Language::Ro => "Romanian",
        Language::Sk => "Slovak",
        Language::Sl => "Slovenian",
        Language::Sv => "Swedish",
        Language::Ru => "Russian",
        Language::Uk => "Ukrainian",
    }
}

pub(super) fn model_label(model: SpeechModel) -> &'static str {
    match model {
        SpeechModel::Whisper => "Whisper small",
        SpeechModel::Canary => "Canary 1B v2",
    }
}

pub(super) fn model_switch_note(wanted: Speech) -> String {
    let from = language_label(wanted.language);
    match (wanted.model, wanted.language) {
        (SpeechModel::Canary, Language::Auto) => {
            "Reset language to English. With Canary, auto-detect always translates to English."
                .to_owned()
        }
        (model, _) => format!(
            "{} doesn't support {from}, so language was reset to English.",
            model_label(model)
        ),
    }
}

pub(super) fn model_help(model: SpeechModel, installed: bool) -> &'static str {
    match (model, installed) {
        (SpeechModel::Whisper, false) => "Not installed. Run vinowhisper-setup to export it.",
        (SpeechModel::Whisper, true) => "5 languages, English translation. Best for English.",
        (SpeechModel::Canary, true) => "25 European languages, translates to English.",
        (SpeechModel::Canary, false) => "Not downloaded. Download it below to use Canary.",
    }
}
