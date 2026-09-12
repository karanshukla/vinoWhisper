use std::fs;
use std::io::{self, BufRead, BufReader, Write};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};
use std::thread;
use std::time::Duration;

const TIMEOUT: Duration = Duration::from_secs(2);

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Request {
    Show,
    Hide,
    Toggle,
    Quit,
    Ping,
}

impl Request {
    const ALL: [Request; 5] = [
        Request::Show,
        Request::Hide,
        Request::Toggle,
        Request::Quit,
        Request::Ping,
    ];

    pub fn as_str(self) -> &'static str {
        match self {
            Request::Show => "show",
            Request::Hide => "hide",
            Request::Toggle => "toggle",
            Request::Quit => "quit",
            Request::Ping => "ping",
        }
    }

    pub fn parse(word: &str) -> Option<Request> {
        Request::ALL
            .into_iter()
            .find(|request| request.as_str() == word)
    }
}

#[derive(Debug)]
pub enum SendError {
    NotRunning,
    Io(io::Error),
}

pub fn socket_path() -> PathBuf {
    match std::env::var_os("XDG_RUNTIME_DIR") {
        Some(dir) if !dir.is_empty() => PathBuf::from(dir).join("vinowhisper-gui.sock"),
        _ => std::env::temp_dir().join(format!(
            "vinowhisper-gui-{}.sock",
            rustix::process::getuid().as_raw()
        )),
    }
}

pub fn send(request: Request) -> Result<(), SendError> {
    send_to(&socket_path(), request)
}

fn send_to(path: &Path, request: Request) -> Result<(), SendError> {
    let mut stream = match UnixStream::connect(path) {
        Ok(stream) => stream,
        Err(err)
            if matches!(
                err.kind(),
                io::ErrorKind::NotFound | io::ErrorKind::ConnectionRefused
            ) =>
        {
            return Err(SendError::NotRunning);
        }
        Err(err) => return Err(SendError::Io(err)),
    };
    stream
        .set_read_timeout(Some(TIMEOUT))
        .map_err(SendError::Io)?;
    writeln!(stream, "{}", request.as_str()).map_err(SendError::Io)?;
    let mut reply = String::new();
    BufReader::new(&stream)
        .read_line(&mut reply)
        .map_err(SendError::Io)?;
    match reply.trim() {
        "ok" => Ok(()),
        other => Err(SendError::Io(io::Error::other(format!(
            "the running instance replied {other:?}"
        )))),
    }
}

pub struct Listener {
    path: PathBuf,
}

impl Drop for Listener {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.path);
    }
}

pub fn listen(on_request: impl Fn(Request) + Send + 'static) -> io::Result<Listener> {
    listen_at(socket_path(), on_request)
}

fn listen_at(path: PathBuf, on_request: impl Fn(Request) + Send + 'static) -> io::Result<Listener> {
    let listener = match UnixListener::bind(&path) {
        Ok(listener) => listener,
        Err(err) if err.kind() == io::ErrorKind::AddrInUse => {
            // Maybe a stale socket from a killed instance: only a live one answers.
            if UnixStream::connect(&path).is_ok() {
                return Err(io::Error::new(
                    io::ErrorKind::AddrInUse,
                    "another vinowhisper-gui is already running",
                ));
            }
            fs::remove_file(&path)?;
            UnixListener::bind(&path)?
        }
        Err(err) => return Err(err),
    };
    thread::Builder::new().name("ipc".into()).spawn(move || {
        for stream in listener.incoming().flatten() {
            serve(stream, &on_request);
        }
    })?;
    Ok(Listener { path })
}

fn serve(stream: UnixStream, on_request: &impl Fn(Request)) {
    let _ = stream.set_read_timeout(Some(TIMEOUT));
    let mut line = String::new();
    if BufReader::new(&stream).read_line(&mut line).is_err() {
        return;
    }
    let reply = match Request::parse(line.trim()) {
        Some(Request::Ping) => "ok",
        Some(request) => {
            on_request(request);
            "ok"
        }
        None => "unknown command",
    };
    let _ = (&stream).write_all(format!("{reply}\n").as_bytes());
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::mpsc;

    fn scratch_socket(name: &str) -> PathBuf {
        let path = std::env::temp_dir().join(format!("vwg-{}-{name}.sock", std::process::id()));
        let _ = fs::remove_file(&path);
        path
    }

    #[test]
    fn a_request_reaches_the_running_instance() {
        let path = scratch_socket("reach");
        let (tx, rx) = mpsc::channel();
        let _listener = listen_at(path.clone(), move |request| tx.send(request).unwrap()).unwrap();

        send_to(&path, Request::Toggle).unwrap();
        assert_eq!(rx.recv_timeout(TIMEOUT).unwrap(), Request::Toggle);
    }

    #[test]
    fn a_ping_is_answered_without_doing_anything() {
        let path = scratch_socket("ping");
        let (tx, rx) = mpsc::channel();
        let _listener = listen_at(path.clone(), move |request| tx.send(request).unwrap()).unwrap();

        send_to(&path, Request::Ping).unwrap();
        assert!(rx.recv_timeout(Duration::from_millis(200)).is_err());
    }

    #[test]
    fn nothing_listening_is_reported_as_not_running() {
        let path = scratch_socket("absent");
        assert!(matches!(
            send_to(&path, Request::Show),
            Err(SendError::NotRunning)
        ));
    }

    #[test]
    fn a_socket_left_by_a_killed_instance_is_reclaimed() {
        let path = scratch_socket("stale");
        drop(UnixListener::bind(&path).unwrap()); // leaves the file behind
        assert!(path.exists());
        assert!(listen_at(path.clone(), |_| {}).is_ok());
    }

    #[test]
    fn a_second_instance_is_refused() {
        let path = scratch_socket("second");
        let _first = listen_at(path.clone(), |_| {}).unwrap();
        let second = listen_at(path.clone(), |_| {});
        assert_eq!(
            second.err().map(|err| err.kind()),
            Some(io::ErrorKind::AddrInUse)
        );
    }

    #[test]
    fn dropping_the_listener_removes_the_socket() {
        let path = scratch_socket("cleanup");
        drop(listen_at(path.clone(), |_| {}).unwrap());
        assert!(!path.exists());
    }

    #[test]
    fn every_request_round_trips_through_its_word() {
        for request in Request::ALL {
            assert_eq!(Request::parse(request.as_str()), Some(request));
        }
        assert_eq!(Request::parse("restart"), None);
    }
}
