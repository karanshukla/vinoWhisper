use super::*;

pub(super) fn source_label(source: Source) -> &'static str {
    match source {
        Source::Output => "Playing",
        Source::Mic => "Microphone",
        Source::Both => "Both",
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
        CleanupMode::Smart => "Only when it hears something to fix",
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
            "Canary can detect the language only to translate it, so Language is now English. \
             Turn on Translate after choosing Detect automatically to keep translating."
                .to_owned()
        }
        (model, _) => format!(
            "{} has no {from}, so Language is now English.",
            model_label(model)
        ),
    }
}

pub(super) fn model_help(model: SpeechModel, installed: bool) -> &'static str {
    match (model, installed) {
        (SpeechModel::Whisper, false) => {
            "Whisper small is not on this machine. Run vinowhisper-setup to export it again \
             (0.9 GB, a few minutes)."
        }
        (SpeechModel::Whisper, true) => {
            "OpenAI's Whisper small, 0.9 GB. Five languages, translates them into English. \
             The default, and the most accurate on English captions."
        }
        (SpeechModel::Canary, true) => {
            "NVIDIA's Canary 1B v2, 0.9 GB. 25 European languages, translates them into \
             English, with or without naming the language. Switching reloads the model."
        }
        (SpeechModel::Canary, false) => {
            "Canary 1B v2 is not downloaded yet: use Download below (0.97 GB on an NPU, \
             1.9 GB otherwise)."
        }
    }
}
