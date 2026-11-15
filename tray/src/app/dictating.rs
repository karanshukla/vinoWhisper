use super::surface::place_pill;
use super::*;

impl App {
    pub(super) fn dictator_is(&self, generation: u64) -> bool {
        self.dictator
            .as_ref()
            .is_some_and(|dictator| dictator.generation == generation)
    }

    pub(super) fn start_dictator(&mut self) {
        if self.dictator.is_some() || self.quitting {
            return;
        }
        let Some(program) = &self.dictate_program else {
            return;
        };
        self.dictator_generation += 1;
        match Dictator::start(program, self.dictator_generation, self.tx.clone()) {
            Ok(dictator) => self.dictator = Some(dictator),
            Err(err) => eprintln!(
                "[vinowhisper-tray] could not start {}: {err}",
                program.display()
            ),
        }
    }

    pub(super) fn tell_dictator(&mut self, command: &str) {
        if command == "start" {
            self.start_dictator();
        }
        let Some(dictator) = &mut self.dictator else {
            let why = if self.dictate_program.is_none() {
                format!(
                    "{} was not found; update vinoWhisper (pip install -U vinowhisper)",
                    session::DICTATE
                )
            } else {
                format!("{} is not running", session::DICTATE)
            };
            self.dictation.fail(why);
            return;
        };
        if let Err(err) = dictator.send(command) {
            self.dictator = None;
            self.dictation
                .fail(format!("lost {}: {err}", session::DICTATE));
        }
    }

    pub(super) fn dictate(&mut self, action: Option<Action>) {
        if action == Some(Action::Start) && self.settings.pause_media {
            self.media.pause();
        }
        if action == Some(Action::Start) {
            self.spacing.started(Instant::now());
        }
        match action {
            Some(Action::Start) => self.tell_dictator("start"),
            Some(Action::HandsFree) => self.tell_dictator("hands-free"),
            Some(Action::Stop) => self.tell_dictator("stop"),
            Some(Action::Paste(text)) => {
                let lead = self.spacing.lead(&text);
                let text = if self.settings.smart_spacing && !lead.is_empty() {
                    format!("{lead}{text}")
                } else {
                    text
                };
                if !self.portal.type_text(text) {
                    self.dictation
                        .pasted(Err("the portal thread is gone".into()));
                }
            }
            None => {}
        }
        self.show_pill();
    }

    pub(super) fn resume_media(&mut self) {
        if !self.dictation.is_busy() {
            self.media.resume();
        }
    }

    pub(super) fn show_pill(&mut self) {
        self.resume_media();
        if let Some(message) = self.dictation.take_announcement() {
            announce::announce(message);
        }
        if self.dictation.pill().is_none() {
            self.pill = None;
            return;
        }
        if self.pill.is_none() {
            self.pill = Some(self.create_layer("vinowhisper-dictation", |layer, app| {
                place_pill(layer, &app.settings, app.visible);
            }));
        }
        self.draw_pill();

        let epoch = self.dictation.epoch();
        if let Some(linger) = self.dictation.linger()
            && self.lingering != Some(epoch)
        {
            self.lingering = Some(epoch);
            let _ = self.handle.insert_source(
                Timer::from_duration(linger),
                move |_, _, app: &mut App| {
                    app.dictation.dismiss(epoch);
                    app.show_pill();
                    app.track_idle();
                    app.sync_tray();
                    TimeoutAction::Drop
                },
            );
        }
    }
}
