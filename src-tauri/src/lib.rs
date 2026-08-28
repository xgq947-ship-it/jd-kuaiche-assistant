mod backend;
mod updater;

use tauri::{
    menu::{Menu, MenuItem},
    tray::TrayIconBuilder,
    Manager, RunEvent, WindowEvent,
};

/// 关窗口不退出应用 —— 这是本工具的核心行为。
///
/// 轮换只在应用进程存活期间进行。macOS 用户习惯点红叉「收起窗口」，若那样就
/// 结束进程，轮换会在用户毫无察觉的情况下停掉。因此关窗口改为隐藏，真正退出
/// 只能走托盘菜单或 Cmd+Q。
fn hide_instead_of_quit(app: &tauri::AppHandle) {
    let Some(window) = app.get_webview_window("main") else {
        return;
    };
    let handle = app.clone();
    window.on_window_event(move |event| {
        if let WindowEvent::CloseRequested { api, .. } = event {
            api.prevent_close();
            if let Some(window) = handle.get_webview_window("main") {
                let _ = window.hide();
            }
        }
    });
}

fn build_tray(app: &tauri::AppHandle) -> tauri::Result<()> {
    let show = MenuItem::with_id(app, "show", "显示主界面", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "退出（会停止轮换）", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&show, &quit])?;

    let mut tray = TrayIconBuilder::with_id("main")
        .menu(&menu)
        .tooltip("京东快车轮换助手")
        .on_menu_event(|app, event| match event.id.as_ref() {
            "show" => {
                if let Some(window) = app.get_webview_window("main") {
                    let _ = window.show();
                    let _ = window.set_focus();
                }
            }
            "quit" => app.exit(0),
            _ => {}
        });
    if let Some(icon) = app.default_window_icon().cloned() {
        tray = tray.icon(icon);
    }
    tray.build(app)?;
    Ok(())
}

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
            let handle = app.handle().clone();
            hide_instead_of_quit(&handle);
            if let Err(err) = build_tray(&handle) {
                // 托盘建不起来不该让应用启不来，但必须留痕：没有托盘时，
                // 用户将无法把隐藏的窗口唤回来。
                log::error!("托盘图标创建失败：{err}");
            }

            // 提前拉起后端：窗口出来时前端就能立刻拿到 endpoint。
            let spawn_handle = handle.clone();
            std::thread::spawn(move || {
                let state = spawn_handle.state::<backend::BackendState>();
                if let Err(err) = backend::start(&spawn_handle, &state) {
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
