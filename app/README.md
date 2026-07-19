# liliclaw 桌面壳（Tauri 2）

App 只是壳：窗口加载本地站点、托盘常驻、系统通知、随开机管理常驻网关。
**关掉 App 不影响员工运行**（agent 活在 launchd 常驻网关里）。

## 构建前置（一次性）

```bash
# Rust 工具链
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
# Tauri CLI
cargo install tauri-cli --version '^2'
```

## 开发 / 打包

```bash
cd app/src-tauri
cargo tauri dev      # 开发：自动拉起本地服务器并开窗口
cargo tauri build    # 打包：产出 .app / .dmg
```

行为：启动时若 8866 未在监听则拉起 `python3 ../../server/server.py 8866`；
窗口加载 http://localhost:8866；关窗最小化到托盘；托盘菜单=打开工作台/退出壳（不动员工）。
