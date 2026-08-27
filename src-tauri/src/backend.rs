//! 拉起并看护 Python 后端。
//!
//! 后端以随机端口监听 127.0.0.1，启动后在 stdout 打印一行
//! `JDKA_ENDPOINT {"port":..,"token":..}`。外壳读到该行才认为就绪，
//! 之后把 endpoint 交给前端。令牌只在本进程与前端之间传递，不落盘。

use serde::{Deserialize, Serialize};
use std::{
    io::{BufRead, BufReader},
    path::PathBuf,
    process::{Child, Command, Stdio},
    sync::{
        mpsc::{self, RecvTimeoutError},
        Mutex,
    },
    time::Duration,
};
use tauri::{AppHandle, Manager, State};

const READY_TIMEOUT: Duration = Duration::from_secs(90);
const ENDPOINT_PREFIX: &str = "JDKA_ENDPOINT ";

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Endpoint {
    pub port: u16,
    pub token: String,
    #[serde(default)]
    pub version: String,
}

#[derive(Default)]
pub struct BackendState {
    endpoint: Mutex<Option<Endpoint>>,
    child: Mutex<Option<Child>>,
}

impl BackendState {
    pub fn shutdown(&self) {
        if let Ok(mut guard) = self.child.lock() {
            if let Some(mut child) = guard.take() {
                let _ = child.kill();
                let _ = child.wait();
            }
        }
    }
}

/// 后端可执行文件位置：打包后在 resources/backend/，开发期回落到本仓库 venv。
fn backend_command(app: &AppHandle) -> Result<Command, String> {
    let exe_name = if cfg!(target_os = "windows") { "jdka-backend.exe" } else { "jdka-backend" };
    let bundled: Option<PathBuf> = app
        .path()
        .resource_dir()
        .ok()
        .map(|dir| dir.join("backend").join(exe_name));

    if let Some(path) = bundled.filter(|p| p.exists()) {
        return Ok(Command::new(path));
    }

    if cfg!(debug_assertions) {
        // 开发期直接用仓库里的 venv，免去每次改 Python 都要重新打包。
        let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .ok_or_else(|| "无法定位项目根目录。".to_string())?
            .to_path_buf();
        let python = if cfg!(target_os = "windows") {
            root.join(".venv/Scripts/python.exe")
        } else {
            root.join(".venv/bin/python")
        };
        if python.exists() {
            let mut cmd = Command::new(python);
            cmd.arg("-m").arg("jdka.cli").current_dir(root);
            return Ok(cmd);
        }
    }

    Err("未找到后端程序，请先执行 npm run backend:build。".to_string())
}

pub fn start(app: &AppHandle, state: &BackendState) -> Result<Endpoint, String> {
    if let Ok(guard) = state.endpoint.lock() {
        if let Some(existing) = guard.clone() {
            return Ok(existing);
        }
    }

    let mut command = backend_command(app)?;
    command
        .arg("ui")
        .arg("--port")
        .arg("0")
        .arg("--no-open")
        .arg("--emit-endpoint")
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .stdin(Stdio::null());

    #[cfg(target_os = "windows")]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        command.creation_flags(CREATE_NO_WINDOW);
    }

    let mut child = command
        .spawn()
        .map_err(|err| format!("无法启动后端：{err}"))?;
    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| "无法读取后端输出。".to_string())?;

    // 在独立线程里读第一行 endpoint，避免后端卡住时冻结主线程。
    let (tx, rx) = mpsc::channel::<Result<Endpoint, String>>();
    std::thread::spawn(move || {
        for line in BufReader::new(stdout).lines() {
            let Ok(line) = line else { break };
            if let Some(rest) = line.strip_prefix(ENDPOINT_PREFIX) {
                let parsed = serde_json::from_str::<Endpoint>(rest)
                    .map_err(|err| format!("后端返回的地址无法解析：{err}"));
                let _ = tx.send(parsed);
                return;
            }
        }
        let _ = tx.send(Err("后端退出前未报告监听地址。".to_string()));
    });

    let endpoint = match rx.recv_timeout(READY_TIMEOUT) {
        Ok(result) => result,
        Err(RecvTimeoutError::Timeout) => Err("后端启动超时。".to_string()),
        Err(RecvTimeoutError::Disconnected) => Err("后端启动失败。".to_string()),
    };

    match endpoint {
        Ok(endpoint) => {
            if let Ok(mut guard) = state.child.lock() {
                *guard = Some(child);
            }
            if let Ok(mut guard) = state.endpoint.lock() {
                *guard = Some(endpoint.clone());
            }
            Ok(endpoint)
        }
        Err(message) => {
            let _ = child.kill();
            let _ = child.wait();
            Err(message)
        }
    }
}

#[tauri::command]
pub fn backend_endpoint(
    app: AppHandle,
    state: State<'_, BackendState>,
) -> Result<Endpoint, String> {
    start(&app, &state)
}
