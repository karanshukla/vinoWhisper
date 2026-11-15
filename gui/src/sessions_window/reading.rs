use super::*;

impl SessionsWindow {
    pub(super) fn reading(&self, path: &std::path::Path) -> bool {
        self.reading.as_ref().is_some_and(|r| r.path == path)
    }

    // One reading at a time: starting one, or Stop, ends the last.
    pub(super) fn read_aloud(&mut self, path: &std::path::Path, part: Part) {
        let was = self.reading.take().map(|r| r.path.clone());
        self.read_error = None;
        if was.as_deref() == Some(path) {
            return;
        }
        let Some(program) = &self.speaker else {
            return;
        };
        match Reading::start(program, path, part) {
            Ok(reading) => self.reading = Some(reading),
            Err(why) => self.read_error = Some(why),
        }
    }

    pub(super) fn stop_reading_unless(&mut self, path: Option<&std::path::Path>) {
        if self
            .reading
            .as_ref()
            .is_some_and(|r| Some(r.path.as_path()) != path)
        {
            self.reading = None;
        }
    }

    pub(super) fn follow_reading(&mut self, ctx: &egui::Context) {
        let Some(reading) = self.reading.as_mut() else {
            return;
        };
        match reading.poll() {
            None => ctx.request_repaint_after(Duration::from_millis(200)),
            Some(result) => {
                self.reading = None;
                self.read_error = result.err();
            }
        }
    }

    pub(super) fn read_notice(&mut self, ui: &mut egui::Ui) {
        if let Some(why) = self.read_error.clone() {
            ui.add_space(12.0);
            if widgets::notice(ui, Tone::Error, &format!("Reading aloud stopped: {why}")) {
                self.read_error = None;
            }
        }
    }
}
