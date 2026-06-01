#!/usr/bin/env python3
"""
LiveTV Recorder — 直播自动录制 + OpenList 存档

依赖：
  pip install requests (或 apk add py3-requests)

用法：
  ./livetv-recorder.py --db /path/to/livetv.db check       # 检查一次状态
  ./livetv-recorder.py --db /path/to/livetv.db daemon      # 持续监听
  ./livetv-recorder.py --db /path/to/livetv.db daemon --openlist-url http://192.168.123.199:5244 --openlist-auth admin:openlist123
"""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

try:
    import requests
except ImportError:
    requests = None


# ── 配置 ──────────────────────────────────────────────────
DB_PATH = "./data/livetv.db"
RECORDINGS_DIR = "./recordings"          # 录制文件存放目录
CHECK_INTERVAL = 120                     # 检查间隔（秒），默认 2 分钟
FFMPEG_BIN = "ffmpeg"
YTDL_BIN = "yt-dlp"

# OpenList 默认地址（可通过命令行覆盖）
OPENLIST_URL = ""
OPENLIST_AUTH = ""                       # "user:pass"


# ── 核心逻辑 ──────────────────────────────────────────────

def get_channels(db_path):
    """从 livetv SQLite 数据库读取频道列表"""
    if not os.path.exists(db_path):
        print(f"[ERROR] 数据库文件不存在: {db_path}")
        return []
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    # channels 表可能叫 channels 或 channel，尝试常见命名
    tables = [row["name"] for row in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    table_name = None
    for name in ["channels", "channel"]:
        if name in tables:
            table_name = name
            break
    if not table_name:
        print(f"[ERROR] 找不到 channels 表，现有表: {tables}")
        conn.close()
        return []
    rows = cur.execute(f"SELECT * FROM {table_name}").fetchall()
    conn.close()
    channels = []
    for row in rows:
        ch = dict(row)
        # 兼容 platform 字段可能不存在的情况
        if "platform" not in ch:
            ch["platform"] = "youtube"
        channels.append(ch)
    return channels


def is_live(channel_url):
    """通过 yt-dlp 检测直播是否在线，返回 (is_live, stream_url, title)"""
    try:
        # 先尝试获取直播流 URL（-g 参数）
        result = subprocess.run(
            [YTDL_BIN, "-g", "-f", "b", channel_url],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0 and result.stdout.strip():
            # 有流 URL → 直播中
            stream_url = result.stdout.strip().split("\n")[0]
            # 获取标题
            title_result = subprocess.run(
                [YTDL_BIN, "--print", "title", channel_url],
                capture_output=True, text=True, timeout=15
            )
            title = title_result.stdout.strip() if title_result.returncode == 0 else "unknown"
            return True, stream_url, title
        else:
            # 无流 URL → 可能离线
            stderr = result.stderr.lower()
            if "offline" in stderr or "not available" in stderr or "no video" in stderr:
                return False, None, None
            # 也可能是直播结束了
            return False, None, None
    except subprocess.TimeoutExpired:
        print(f"[WARN] yt-dlp 超时: {channel_url}")
        return False, None, None
    except FileNotFoundError:
        print(f"[ERROR] 找不到 {YTDL_BIN}，请先安装")
        sys.exit(1)
    except Exception as e:
        print(f"[ERROR] yt-dlp 异常: {e}")
        return False, None, None


def get_recording_path(channel, recordings_dir):
    """生成录制文件路径：{recordings_dir}/{platform}/{channel_name}/{date}.ts"""
    platform = channel.get("platform", "youtube")
    name = channel.get("name", "unknown")
    safe_name = "".join(c if c.isalnum() or c in " _-" else "_" for c in name).strip()
    safe_name = safe_name or f"channel_{channel['id']}"
    date_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    ch_dir = Path(recordings_dir) / platform / safe_name
    ch_dir.mkdir(parents=True, exist_ok=True)
    return str(ch_dir / f"{date_str}.ts")


def start_recording(stream_url, output_path):
    """启动 ffmpeg 录制进程"""
    # 注意：使用 -t 限制录制时长？不限制，录到直播结束
    # 使用 -c copy 避免转码
    log_path = output_path + ".log"
    with open(log_path, "w") as log_fp:
        proc = subprocess.Popen(
            [FFMPEG_BIN, "-y", "-i", stream_url, "-c", "copy", "-f", "mpegts", output_path],
            stdout=log_fp, stderr=subprocess.STDOUT
        )
    return proc


def upload_to_openlist(file_path, openlist_url, auth):
    """上传文件到 OpenList"""
    if not (openlist_url and auth):
        return
    if requests is None:
        print("[WARN] requests 未安装，跳过 OpenList 上传")
        return
    username, password = auth.split(":", 1)
    try:
        # 构建上传 URL
        base = openlist_url.rstrip("/")
        upload_api = f"{base}/api/fs/upload"
        # 先获取 token
        sess = requests.Session()
        sess.auth = (username, password)
        # OpenList 的 API 上传
        filename = os.path.basename(file_path)
        dir_path = f"/livetv-recordings/{os.path.basename(os.path.dirname(os.path.dirname(file_path)))}/{os.path.basename(os.path.dirname(file_path))}"
        with open(file_path, "rb") as f:
            resp = sess.put(
                f"{base}/api/fs/upload?path={urllib.parse.quote(dir_path)}&name={urllib.parse.quote(filename)}",
                data=f,
                timeout=300
            )
        if resp.status_code in (200, 201):
            print(f"[OK] 上传 OpenList 成功: {dir_path}/{filename}")
        else:
            print(f"[WARN] 上传 OpenList 返回 {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        print(f"[ERROR] 上传 OpenList 失败: {e}")


def check_and_record(channel, recordings_dir, openlist_url, openlist_auth, active_recordings):
    """检查一个频道，如果直播中且未在录制则开始录制"""
    ch_id = channel["id"]
    ch_url = channel["url"]
    ch_name = channel.get("name", f"channel_{ch_id}")
    platform = channel.get("platform", "youtube")

    # 检查是否已在录制
    if ch_id in active_recordings:
        proc = active_recordings[ch_id]["proc"]
        ret = proc.poll()
        if ret is None:
            # 仍在录制中
            return
        else:
            # 录制结束
            output_path = active_recordings[ch_id]["output"]
            print(f"[END] 录制结束 ({ret}): {ch_name} -> {output_path}")
            # 上传到 OpenList
            if os.path.exists(output_path):
                upload_to_openlist(output_path, openlist_url, openlist_auth)
            del active_recordings[ch_id]

    # 检测直播状态
    print(f"[CHECK] {platform}/{ch_name} ...", end=" ", flush=True)
    live, stream_url, title = is_live(ch_url)
    if live:
        print(f"🟢 直播中: {title}")
        output_path = get_recording_path(channel, recordings_dir)
        proc = start_recording(stream_url, output_path)
        active_recordings[ch_id] = {
            "proc": proc,
            "output": output_path,
            "channel_name": ch_name,
            "started_at": datetime.now(),
        }
        print(f"[REC] 开始录制: {ch_name} -> {output_path}")
    else:
        print("⚫ 未直播")


def run_daemon(db_path, recordings_dir, openlist_url, openlist_auth, interval):
    """持续监听模式"""
    print(f"[DAEMON] 开始监听直播，数据库={db_path}")
    print(f"[DAEMON] 录制目录={recordings_dir}")
    print(f"[DAEMON] 检查间隔={interval}秒")
    if openlist_url:
        print(f"[DAEMON] OpenList 存档: {openlist_url}")

    active_recordings = {}
    while True:
        channels = get_channels(db_path)
        print(f"\n{'='*50}")
        print(f"[DAEMON] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} — 共 {len(channels)} 个频道")
        for ch in channels:
            check_and_record(ch, recordings_dir, openlist_url, openlist_auth, active_recordings)
        print(f"[DAEMON] 当前录制: {len(active_recordings)} 个")
        time.sleep(interval)


def run_check(db_path, recordings_dir):
    """单次检查模式"""
    channels = get_channels(db_path)
    print(f"共 {len(channels)} 个频道:\n")
    for ch in channels:
        platform = ch.get("platform", "youtube")
        ch_name = ch.get("name", f"channel_{ch['id']}")
        ch_url = ch["url"]
        print(f"[{platform}] {ch_name}")
        print(f"   URL: {ch_url}")
        live, stream_url, title = is_live(ch_url)
        if live:
            print(f"   🟢 直播中: {title}")
        else:
            print(f"   ⚫ 未直播")
        print()


def main():
    parser = argparse.ArgumentParser(description="LiveTV Recorder — 直播录制 + OpenList 存档")
    parser.add_argument("--db", default=DB_PATH, help=f"livetv 数据库路径 (默认: {DB_PATH})")
    parser.add_argument("--recordings-dir", default=RECORDINGS_DIR, help=f"录制文件目录 (默认: {RECORDINGS_DIR})")
    parser.add_argument("--openlist-url", default=OPENLIST_URL, help="OpenList 地址 (如 http://192.168.123.199:5244)")
    parser.add_argument("--openlist-auth", default=OPENLIST_AUTH, help="OpenList 认证 user:pass")
    parser.add_argument("--interval", type=int, default=CHECK_INTERVAL, help=f"检查间隔秒数 (默认: {CHECK_INTERVAL})")
    parser.add_argument("mode", choices=["check", "daemon"], help="check=单次检查, daemon=持续监听")
    args = parser.parse_args()

    if args.mode == "check":
        run_check(args.db, args.recordings_dir)
    elif args.mode == "daemon":
        run_daemon(args.db, args.recordings_dir, args.openlist_url, args.openlist_auth, args.interval)


if __name__ == "__main__":
    main()
