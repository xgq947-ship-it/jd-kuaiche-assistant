//! 应用内更新：查 GitHub Release → 下载 → 校验 SHA-256 → 交给系统安装。
//!
//! 安全约束（与 reverse-prompt 保持一致）：
//! - 下载地址必须落在本仓库的 releases/download 路径下，杜绝被引导到任意 URL。
//! - 必须校验 GitHub 提供的 SHA-256，校验不通过立刻删除临时文件。
//! - 不静默替换正在运行的程序：在 macOS 上改写 .app 会破坏签名与公证，
//!   因此下载完成后交由系统打开安装包，由用户确认。

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    fs,
    io::Write,
    path::PathBuf,
    process::Command,
    sync::Mutex,
    time::Duration,
};
use tauri::{AppHandle, Emitter, Manager};

const REPO: &str = "xgq947-ship-it/jd-kuaiche-assistant";
const RELEASE_HOST: &str = "github.com";
const MAX_UPDATE_BYTES: u64 = 512 * 1024 * 1024;
const PROGRESS_EVENT: &str = "update-download-progress";

#[derive(Default)]
pub struct UpdateState {
    downloaded: Mutex<Option<PathBuf>>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct DownloadRequest {
    url: String,
    digest: String,
    #[serde(default)]
    expected_size: u64,
}

#[derive(Debug, Serialize, Clone)]
#[serde(rename_all = "camelCase")]
struct Progress {
    downloaded_bytes: u64,
    total_bytes: u64,
    percent: u8,
}

fn normalized_digest(value: &str) -> Result<String, String> {
    let digest = value.strip_prefix("sha256:").unwrap_or(value).to_ascii_lowercase();
    if digest.len() != 64 || !digest.bytes().all(|b| b.is_ascii_hexdigit()) {
        return Err("更新包 SHA-256 校验值无效。".to_string());
    }
    Ok(digest)
}

/// 只允许下载本仓库 Release 下的资产。
fn validate_url(raw: &str) -> Result<reqwest::Url, String> {
    let url = reqwest::Url::parse(raw).map_err(|_| "更新地址无效。".to_string())?;
    if url.scheme() != "https" {
        return Err("更新地址必须使用 HTTPS。".to_string());
    }
    if url.host_str() != Some(RELEASE_HOST) {
        return Err("更新地址不在受信任的发布域名内。".to_string());
    }
    let expected = format!("/{REPO}/releases/download/");
    if !url.path().starts_with(&expected) {
        return Err("更新地址不在本应用的发布路径内。".to_string());
    }
    Ok(url)
}

#[tauri::command]
pub async fn update_download(
    app: AppHandle,
    request: DownloadRequest,
) -> Result<u64, String> {
    let url = validate_url(&request.url)?;
    let expected_digest = normalized_digest(&request.digest)?;

    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(1800))
        .build()
        .map_err(|err| format!("无法初始化下载器：{err}"))?;
    let mut response = client
        .get(url)
        .send()
        .await
        .map_err(|err| format!("下载失败：{err}"))?
        .error_for_status()
        .map_err(|err| format!("下载失败：{err}"))?;

    let total = response.content_length().unwrap_or(request.expected_size);
    if total > MAX_UPDATE_BYTES {
        return Err("更新包体积异常，已中止。".to_string());
    }

    let dir = app
        .path()
        .app_cache_dir()
        .map_err(|_| "无法定位缓存目录。".to_string())?
        .join("updates");
    fs::create_dir_all(&dir).map_err(|_| "无法创建缓存目录。".to_string())?;
    let name = response
        .url()
        .path_segments()
        .and_then(|mut s| s.next_back())
        .filter(|s| !s.is_empty())
        .unwrap_or("update.bin")
        .to_string();
    let target = dir.join(&name);

    let mut file = fs::File::create(&target).map_err(|_| "无法写入更新包。".to_string())?;
    let mut hasher = Sha256::new();
    let mut written: u64 = 0;
    let mut last_percent = u8::MAX;

    while let Some(chunk) = response
        .chunk()
        .await
        .map_err(|err| format!("下载中断：{err}"))?
    {
        written += chunk.len() as u64;
        if written > MAX_UPDATE_BYTES {
            let _ = fs::remove_file(&target);
            return Err("更新包体积异常，已中止。".to_string());
        }
        hasher.update(&chunk);
        file.write_all(&chunk).map_err(|_| "写入更新包失败。".to_string())?;
        let percent = if total > 0 {
            ((written as f64 / total as f64) * 100.0).min(100.0) as u8
        } else {
            0
        };
        if percent != last_percent {
            last_percent = percent;
            let _ = app.emit(
                PROGRESS_EVENT,
                Progress { downloaded_bytes: written, total_bytes: total, percent },
            );
        }
    }
    file.flush().map_err(|_| "写入更新包失败。".to_string())?;
    drop(file);

    let actual = format!("{:x}", hasher.finalize());
    if actual != expected_digest {
        let _ = fs::remove_file(&target);
        return Err("更新包校验失败，已删除该文件。".to_string());
    }

    if let Ok(mut guard) = app.state::<UpdateState>().downloaded.lock() {
        *guard = Some(target);
    }
    Ok(written)
}

/// 打开已下载并校验通过的安装包，交由系统完成安装。
#[tauri::command]
pub fn update_install(app: AppHandle) -> Result<(), String> {
    let path = app
        .state::<UpdateState>()
        .downloaded
        .lock()
        .ok()
        .and_then(|guard| guard.clone())
        .ok_or_else(|| "尚未下载更新包。".to_string())?;
    if !path.exists() {
        return Err("更新包已不存在，请重新下载。".to_string());
    }
    open_path(&path)
}

fn open_path(path: &PathBuf) -> Result<(), String> {
    #[cfg(target_os = "macos")]
    let status = Command::new("/usr/bin/open").arg(path).status();
    #[cfg(target_os = "windows")]
    let status = Command::new("cmd").args(["/C", "start", ""]).arg(path).status();
    #[cfg(all(not(target_os = "macos"), not(target_os = "windows")))]
    let status = Command::new("xdg-open").arg(path).status();

    match status {
        Ok(code) if code.success() => Ok(()),
        _ => Err("无法打开更新包，请手动前往下载页面。".to_string()),
    }
}

/// 在系统默认浏览器打开发布页（前端的“前往下载”按钮）。
#[tauri::command]
pub fn open_release_page(url: String) -> Result<(), String> {
    let parsed = reqwest::Url::parse(&url).map_err(|_| "地址无效。".to_string())?;
    if parsed.scheme() != "https" || parsed.host_str() != Some(RELEASE_HOST) {
        return Err("只允许打开本项目的发布页面。".to_string());
    }
    #[cfg(target_os = "macos")]
    let status = Command::new("/usr/bin/open").arg(parsed.as_str()).status();
    #[cfg(target_os = "windows")]
    let status = Command::new("cmd").args(["/C", "start", ""]).arg(parsed.as_str()).status();
    #[cfg(all(not(target_os = "macos"), not(target_os = "windows")))]
    let status = Command::new("xdg-open").arg(parsed.as_str()).status();

    match status {
        Ok(code) if code.success() => Ok(()),
        _ => Err("无法打开浏览器。".to_string()),
    }
}
