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
            "Canary only supports automatic language detection when translating, so Language was \
             reset to English. Enable Translate to English after selecting Detect automatically \
             to keep translating."
                .to_owned()
        }
        (model, _) => format!(
            "{} does not support {from}, so Language was reset to English.",
            model_label(model)
        ),
    }
}

pub(super) fn model_help(model: SpeechModel, installed: bool) -> &'static str {
    match (model, installed) {
        (SpeechModel::Whisper, false) => {
            "Whisper small is not installed. Run vinowhisper-setup to export it (0.9 GB, takes a \
             few minutes)."
        }
        (SpeechModel::Whisper, true) => {
            "OpenAI Whisper small (0.9 GB). Supports 5 languages and translates into English. \
             Default model and most accurate for English captions."
        }
        (SpeechModel::Canary, true) => {
            "NVIDIA Canary 1B v2 (0.9 GB). Supports 25 European languages and translates into \
             English, with or without specifying the source language. Switching reloads the model."
        }
        (SpeechModel::Canary, false) => {
            "Canary 1B v2 is not downloaded yet. Click Download below to install it (0.97 GB for \
             NPU, 1.9 GB for CPU/GPU)."
        }
    }
}
