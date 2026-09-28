// backend.rs — owns the jobscout Python backend as a child process.
//
// The desktop app is a thin shell: the FastAPI server (repo checkout in
// dev via .venv, PyInstaller-frozen binary in release) is spawned on a
// free local port; the webview window loads it once it answers. The
// child is killed when the app exits.
//
// Backend resolution order:
//   1. JOBSCOUT_BIN env          — explicit override (npm scripts set it)
//   2. <resource_dir>/backend/jobscout-server — frozen binary (release)
//   3. .venv/bin/jobscout found by walking up from the exe dir (dev)

use std::io::{BufRead, BufReader};
use std::net::TcpListener;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::{Duration, Instant};


/// The live backend child + the port it serves on.
pub struct Backend {
    pub child: Child,
    pub port: u16,
}

pub struct BackendState(pub Mutex<Option<Backend>>);

/// Pick a free localhost port (bind :0, read the assigned port, release).
pub fn free_port() -> u16 {
    TcpListener::bind("127.0.0.1:0")
        .expect("bind :0 for a free port")
        .local_addr()
        .expect("local addr")
        .port()
}

/// Resolve the backend executable + args, per the order in the module docs.
fn backend_command(port: u16) -> Command {
    let mut cmd = Command::new(resolve_backend_bin());
    cmd.args(["serve", "--port", &port.to_string()]);
    cmd
}

fn resolve_backend_bin() -> PathBuf {
    // 1. explicit override
    if let Ok(bin) = std::env::var("JOBSCOUT_BIN") {
        let p = PathBuf::from(&bin);
        if p.is_file() {
            return p;
        }
        log::warn!("JOBSCOUT_BIN={} is not a file — falling back", bin);
    }
    // 2. release: frozen binary in resources
    // (resolved by the caller with the app handle — see spawn_backend)
    // 3. dev: walk up from the exe dir to the repo checkout's venv
    if let Some(exe) = std::env::current_exe().ok() {
        for dir in exe.ancestors().skip(1) {
            let candidate = dir.join(".venv/bin/jobscout");
            if candidate.is_file() {
                return candidate;
            }
        }
    }
    // 4. walk up from the CWD as well (tauri dev runs cargo from src-tauri)
    if let Ok(cwd) = std::env::current_dir() {
        for dir in cwd.ancestors() {
            let candidate = dir.join(".venv/bin/jobscout");
            if candidate.is_file() {
                return candidate;
            }
        }
    }
    PathBuf::from("jobscout") // PATH — last resort
}

/// Spawn the backend. `frozen` is the release (bin, home) pair, if any.
pub fn spawn_backend(
    port: u16,
    frozen: Option<(PathBuf, PathBuf)>,
) -> std::io::Result<Child> {
    let mut cmd = backend_command(port);
    if let Some((bin, home)) = frozen {
        // first-run bootstrap (idempotent): seeds config + master resume
        // from bundled defaults and initialises the database
        match Command::new(&bin)
            .arg("init-home")
            .env("JOBSCOUT_HOME", &home)
            .output()
        {
            Ok(out) => {
                let text = String::from_utf8_lossy(&out.stdout);
                for line in text.lines().filter(|l| !l.is_empty()) {
                    log::info!("[init-home] {line}");
                }
                if !out.status.success() {
                    log::error!(
                        "[init-home] exit {:?}: {}",
                        out.status.code(),
                        String::from_utf8_lossy(&out.stderr)
                    );
                }
            }
            Err(e) => log::error!("[init-home] failed to run: {e}"),
        }
        // release build: frozen binary, runtime relocated into OS app-data
        cmd = Command::new(bin);
        cmd.args(["serve", "--port", &port.to_string()]);
        cmd.env("JOBSCOUT_HOME", home);
        // bundle a JobPilot sidecar later by shipping it in resources and
        // pointing the backend at it:
        if let Ok(sidecar) = std::env::var("JOBSCOUT_SIDECAR_BIN") {
            cmd.env("JOBSCOUT_SIDECAR_BIN", sidecar);
        }
    }
    let mut child = cmd
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()?;
    let pid = child.id();
    // drain stdout/stderr so the pipe never fills and blocks the backend
    if let Some(out) = child.stdout.take() {
        std::thread::spawn(move || {
            for line in BufReader::new(out).lines().map_while(Result::ok) {
                log::info!("[backend:{pid}] {line}");
            }
        });
    }
    if let Some(err) = child.stderr.take() {
        std::thread::spawn(move || {
            for line in BufReader::new(err).lines().map_while(Result::ok) {
                log::warn!("[backend:{pid}] {line}");
            }
        });
    }
    Ok(child)
}

/// Wait until the port answers TCP connects (uvicorn listening).
pub fn wait_ready(port: u16, timeout: Duration) -> bool {
    let deadline = Instant::now() + timeout;
    while Instant::now() < deadline {
        if std::net::TcpStream::connect(("127.0.0.1", port)).is_ok() {
            return true;
        }
        std::thread::sleep(Duration::from_millis(150));
    }
    false
}

/// Kill the managed backend (app exit). Best effort.
pub fn shutdown(state: &BackendState) {
    if let Ok(mut guard) = state.0.lock() {
        if let Some(mut backend) = guard.take() {
            let pid = backend.child.id();
            log::info!("stopping backend (pid {pid}, port {})", backend.port);
            let _ = backend.child.kill();
            let _ = backend.child.wait();
        }
    }
}
