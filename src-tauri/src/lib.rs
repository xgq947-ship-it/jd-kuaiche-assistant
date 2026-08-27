mod backend;
mod updater;

use tauri::{Manager, RunEvent};

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .manage(backend::BackendState::default())
        .manage(updater::UpdateState::default())
        .invoke_handler(tauri::generate_handler![
            backend::backend_endpoint,
            updater::update_download,
            updater::update_install,
            updater::open_release_page,
        ])
        .setup(|app| {
            // 提前拉起后端：窗口出来时前端就能立刻拿到 endpoint。
            let handle = app.handle().clone();
            std::thread::spawn(move || {
                let state = handle.state::<backend::BackendState>();
                if let Err(err) = backend::start(&handle, &state) {
                    log::error!("后端启动失败：{err}");
                }
            });
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("无法初始化应用")
        .run(|app, event| {
            // 退出前务必收掉后端与它拉起的后台浏览器，避免留下孤儿进程。
            if let RunEvent::Exit = event {
                app.state::<backend::BackendState>().shutdown();
            }
        });
}
