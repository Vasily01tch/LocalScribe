use std::{io::{BufRead, BufReader}, process::{Child, Command, Stdio}, sync::{Mutex, atomic::{AtomicBool, Ordering}}};
use tauri::{Emitter, Manager, State};
use serde::Serialize;
struct ExitGuard { enabled: AtomicBool, allow: AtomicBool }
#[tauri::command] fn enable_exit_guard(guard: State<ExitGuard>) { guard.enabled.store(true, Ordering::SeqCst); }
#[tauri::command] fn finish_exit(app: tauri::AppHandle, guard: State<ExitGuard>) { guard.allow.store(true, Ordering::SeqCst); app.exit(0); }
struct Service { child: Mutex<Child>, port: u16, token: String }
#[derive(Serialize)] struct Connection { port: u16, token: String }
#[tauri::command] fn connection(s: State<Service>) -> Connection { Connection { port:s.port, token:s.token.clone() } }
fn main() {
    tauri::Builder::default().plugin(tauri_plugin_dialog::init()).invoke_handler(tauri::generate_handler![connection, enable_exit_guard, finish_exit])
    .setup(|app| {
        let resources = app.path().resource_dir()?.join("resources");
        let token = uuid::Uuid::new_v4().to_string();
        let mut child = Command::new(resources.join("server/server"))
            .env("LOCALSCRIBE_TOKEN", &token).env("LOCALSCRIBE_RECORDER", resources.join("recorder"))
            .env("LOCALSCRIBE_MODEL", resources.join("model"))
            .env("PATH", format!("{}:/usr/bin:/bin:/usr/sbin:/sbin",resources.join("bin").display()))
            .stdout(Stdio::piped()).stderr(Stdio::inherit()).spawn()?;
        let stdout = child.stdout.take().ok_or("service stdout missing")?;
        let mut reader = BufReader::new(stdout); let mut line=String::new(); reader.read_line(&mut line)?;
        let value:serde_json::Value=serde_json::from_str(&line)?;
        let port=value["port"].as_u64().ok_or("service failed to start")? as u16;
        std::thread::spawn(move || { for _ in reader.lines() {} });
        app.manage(Service { child:Mutex::new(child), port, token });
        app.manage(ExitGuard { enabled: AtomicBool::new(false), allow: AtomicBool::new(false) }); Ok(())
    }).build(tauri::generate_context!()).expect("failed to start LocalScribe")
    .run(|app,event| {
        match event {
            tauri::RunEvent::ExitRequested { api, .. } => {
                let guard = app.state::<ExitGuard>();
                if guard.enabled.load(Ordering::SeqCst) && !guard.allow.load(Ordering::SeqCst) && !app.webview_windows().is_empty() {
                    api.prevent_exit();
                    let _ = app.emit("localscribe:before-exit", ());
                }
            }
            tauri::RunEvent::Exit => {
                if let Some(service)=app.try_state::<Service>() {
                    let mut child=service.child.lock().unwrap();
                    unsafe { libc::kill(child.id() as i32, libc::SIGTERM); }
                    let _=child.wait();
                }
            }
            _ => {}
        }
    });
}
