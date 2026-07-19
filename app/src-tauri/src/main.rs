// liliclaw 桌面壳：窗口 + 托盘。铁律：App 生命周期与 agent 解耦——
// 常驻网关由 launchd 托管，这里只负责「看」和「配」，退出壳不影响员工运行。
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::net::TcpStream;
use std::process::Command;
use tauri::{
    menu::{Menu, MenuItem},
    tray::TrayIconBuilder,
    Manager,
};

fn server_alive() -> bool {
    TcpStream::connect(("127.0.0.1", 8866)).is_ok()
}

fn ensure_server(app: &tauri::AppHandle) {
    if server_alive() {
        return;
    }
    // 站点服务器随壳拉起（常驻网关不归壳管，由 launchd 保活）
    let root = app
        .path()
        .resource_dir()
        .ok()
        .and_then(|p| p.parent().map(|x| x.to_path_buf()));
    let script = std::env::var("LILICLAW_SERVER")
        .unwrap_or_else(|_| "../../server/server.py".into());
    let _ = Command::new("python3")
        .arg(script)
        .arg("8866")
        .current_dir(root.unwrap_or_else(|| ".".into()))
        .spawn();
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_shell::init())
        .setup(|app| {
            ensure_server(app.handle());
            let open = MenuItem::with_id(app, "open", "打开工作台", true, None::<&str>)?;
            let quit = MenuItem::with_id(app, "quit", "退出壳（员工照常运行）", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&open, &quit])?;
            TrayIconBuilder::new()
                .icon(app.default_window_icon().unwrap().clone())
                .menu(&menu)
                .on_menu_event(|app, event| match event.id.as_ref() {
                    "open" => {
                        if let Some(w) = app.get_webview_window("main") {
                            let _ = w.show();
                            let _ = w.set_focus();
                        }
                    }
                    "quit" => app.exit(0),
                    _ => {}
                })
                .build(app)?;
            Ok(())
        })
        .on_window_event(|window, event| {
            // 关窗 = 最小化到托盘，不退出（员工与壳都还在）
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                let _ = window.hide();
                api.prevent_close();
            }
        })
        .run(tauri::generate_context!())
        .expect("liliclaw 壳启动失败");
}
