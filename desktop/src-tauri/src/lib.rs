// lib.rs — the jobscout desktop shell.
//
// Flow: pick a free port → spawn the Python backend (frozen binary in
// release, the repo's .venv in dev) → wait for it to listen → open the
// webview window on it → kill the backend when the app exits.

use std::fs::OpenOptions;
use std::io::Write;
use std::sync::Mutex;
use std::time::Duration;

use tauri::{Manager, RunEvent, WebviewUrl, WebviewWindowBuilder};

mod backend;

use backend::BackendState;

// ── logging: stderr (dev terminal) + app-data/logs/desktop.log ───────────
struct FileLogger {
    file: Mutex<std::fs::File>,
}

impl log::Log for FileLogger {
    fn enabled(&self, metadata: &log::Metadata) -> bool {
        metadata.level() <= log::Level::Info
    }
    fn log(&self, record: &log::Record) {
        if !self.enabled(record.metadata()) {
            return;
        }
        let line = format!("[{}] {}", record.level(), record.args());
        eprintln!("{line}");
        if let Ok(mut file) = self.file.lock() {
            let _ = writeln!(file, "{line}");
            let _ = file.flush();
        }
    }
    fn flush(&self) {
        if let Ok(mut file) = self.file.lock() {
            let _ = file.flush();
        }
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .setup(|app| {
            // logger first — everything after lands in app-data/logs/desktop.log
            if let Ok(data_dir) = app.path().app_data_dir() {
                let log_dir = data_dir.join("logs");
                let _ = std::fs::create_dir_all(&log_dir);
                if let Ok(file) = OpenOptions::new()
                    .create(true)
                    .append(true)
                    .open(log_dir.join("desktop.log"))
                {
                    let _ = log::set_boxed_logger(Box::new(FileLogger {
                        file: Mutex::new(file),
                    }));
                    log::set_max_level(log::LevelFilter::Info);
                }
            }
            log::info!("jobscout desktop starting");

            let state = BackendState(std::sync::Mutex::new(None));
            app.manage(state);

            let handle = app.handle().clone();
            std::thread::spawn(move || {
                let port = backend::free_port();
                log::info!("jobscout backend starting on 127.0.0.1:{port}");

                // release: frozen binary in resources (+ app-data home);
                // dev: env/venv resolution with repo-relative paths
                let frozen = handle
                    .path()
                    .resource_dir()
                    .ok()
                    .and_then(|r| {
                        let bin = r.join("backend").join("jobscout-server");
                        bin.is_file().then_some(bin)
                    })
                    .zip(
                        handle
                            .path()
                            .app_data_dir()
                            .ok()
                            .map(|home| {
                                let _ = std::fs::create_dir_all(&home);
                                home
                            }),
                    );

                match backend::spawn_backend(port, frozen) {
                    Ok(mut child) => {
                        if backend::wait_ready(port, Duration::from_secs(60)) {
                            log::info!("backend ready on 127.0.0.1:{port}");
                            if let Ok(mut guard) =
                                handle.state::<BackendState>().0.lock()
                            {
                                *guard = Some(backend::Backend { child, port });
                            }
                            open_main_window(&handle, port);
                        } else {
                            log::error!(
                                "backend did not become ready on port {port} in 60s"
                            );
                            let _ = child.kill();
                            let _ = child.wait();
                            open_error_window(
                                &handle,
                                "backend did not become ready in 60 seconds \
                                 (see the dev terminal for backend logs)",
                            );
                        }
                    }
                    Err(e) => {
                        log::error!("failed to spawn jobscout backend: {e}");
                        open_error_window(&handle, &e.to_string());
                    }
                }
            });
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building jobscout desktop")
        .run(|handle, event| {
            // last window closed → app exit → the backend must not outlive us
            if let RunEvent::Exit = event {
                backend::shutdown(&handle.state::<BackendState>());
            }
        });
}

fn open_main_window(handle: &tauri::AppHandle, port: u16) {
    let url: tauri::Url = format!("http://127.0.0.1:{port}")
        .parse()
        .expect("backend url parses");
    let result = WebviewWindowBuilder::new(
        handle,
        "main",
        WebviewUrl::External(url),
    )
    .title("jobscout")
    .inner_size(1440.0, 900.0)
    .min_inner_size(960.0, 600.0)
    .build();
    if let Err(e) = result {
        log::error!("window build failed: {e}");
    }
}

fn open_error_window(handle: &tauri::AppHandle, message: &str) {
    let html = format!(
        "<html><body style='font: 14px -apple-system; padding: 40px; \
         background: #0a1220; color: #e2e8f0'><h2>jobscout backend failed to start</h2>\
         <pre style='white-space: pre-wrap'>{message}</pre>\
         <p>Dev mode expects the repo checkout with .venv next to desktop/.<br>\
         Set JOBSCOUT_BIN to the backend executable to override.</p></body></html>"
    );
    let url = WebviewUrl::App(html.into());
    let _ = WebviewWindowBuilder::new(handle, "error", url)
        .title("jobscout — backend error")
        .inner_size(640.0, 320.0)
        .build();
}
