#!/usr/bin/env python3
import configparser
import fcntl
import json
import os
import pty
import re
import shlex
import shutil
import socket
import subprocess
import threading
import time
import termios
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import Request, urlopen


CONFIG_PATH = Path(os.environ.get("MIDO_WIFI_PORTAL_CONFIG", "/etc/mido-wifi-portal/config.json"))
STATE_LOCK = threading.Lock()
STATE = {
    "last_scan": [],
    "last_scan_at": 0.0,
    "last_message": "",
    "hotspot_active": False,
    "suppress_hotspot_until": 0.0,
}
GPIO_LOCK = threading.Lock()
GPIO_HOLDS = {}
STATUS_CACHE_LOCK = threading.Lock()
STATUS_CACHE = {
    "slow_data": {},
    "updated_at": 0.0,
    "refreshing": False,
    "last_error": "",
}
STATUS_CACHE_TTL = 15.0
TERMINAL_LOCK = threading.Lock()
TERMINAL_SESSIONS = {}
TERMINAL_MAX_BUFFER = 262144
TERMINAL_IDLE_TIMEOUT = 1800
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

DEFAULT_CONFIG = {
    "wifi_interface": "wlan0",
    "hotspot_connection": "mido-setup-ap",
    "hotspot_ssid": "mido-setup",
    "hotspot_password": "12345678",
    "bind_host": "0.0.0.0",
    "bind_port": 80,
    "check_host": "223.5.5.5",
    "check_interval": 15,
    "offline_threshold": 2,
    "online_threshold": 2,
    "reconnect_grace_seconds": 45,
}

SAFE_GPIO_LINES = [
    {"chip": "gpiochip0", "line": 61, "label": "GPIO61", "origin": "显示同步 / TE", "group": "显示"},
    {"chip": "gpiochip0", "line": 64, "label": "GPIO64", "origin": "触摸复位", "group": "触摸"},
    {"chip": "gpiochip0", "line": 65, "label": "GPIO65", "origin": "触摸中断", "group": "触摸"},
    {"chip": "gpiochip0", "line": 85, "label": "GPIO85", "origin": "侧键 / 音量上", "group": "侧键"},
    {"chip": "gpiochip0", "line": 135, "label": "GPIO135", "origin": "指纹 SPI 复用脚", "group": "指纹"},
    {"chip": "gpiochip0", "line": 136, "label": "GPIO136", "origin": "指纹 SPI 复用脚", "group": "指纹"},
    {"chip": "gpiochip0", "line": 137, "label": "GPIO137", "origin": "指纹 SPI 复用脚", "group": "指纹"},
    {"chip": "gpiochip0", "line": 138, "label": "GPIO138", "origin": "指纹 SPI 复用脚", "group": "指纹"},
]


def run(cmd, check=True):
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def nmcli(*args, check=True):
    return run(["nmcli", *args], check=check)


def connectivity_state():
    proc = run(["nmcli", "networking", "connectivity"], check=False)
    state = proc.stdout.strip().lower()
    return state or "unknown"


def load_config():
    cfg = DEFAULT_CONFIG.copy()
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open("r", encoding="utf-8") as fp:
            cfg.update(json.load(fp))
    return cfg


CONFIG = load_config()
GPIO_PWM_SCRIPT = Path(__file__).resolve().with_name("gpio64_led_pwm.py")
BOOT_LED_SERVICE_NAME = "mido-gpio64-led.service"
BOOT_LED_SERVICE_PATH = Path("/etc/systemd/system") / BOOT_LED_SERVICE_NAME
BOOT_LED_DEFAULT = {
    "chip": "/dev/gpiochip0",
    "line": 64,
    "duty": 10.0,
    "hz": 100.0,
    "auto_off_seconds": 300.0,
    "active_low": False,
}
FAN_CONTROL_SERVICE_NAME = "mido-fan-control.service"
FAN_CONTROL_SERVICE_PATH = Path("/etc/systemd/system") / FAN_CONTROL_SERVICE_NAME
FAN_CONTROL_CONFIG_PATH = Path("/etc/mido-wifi-portal/fan-control.json")
FAN_CONTROL_DEFAULT = {
    "enabled": True,
    "chip": "/dev/gpiochip0",
    "line": 65,
    "start_temp": 55.0,
    "full_temp": 75.0,
    "min_duty": 25.0,
    "max_duty": 100.0,
    "hz": 100.0,
    "poll_interval": 3.0,
    "active_low": False,
}
ESP_GPIO_LINK_SERVICE_NAME = "mido-esp-gpio-link.service"
ESP_GPIO_LINK_SERVICE_PATH = Path("/etc/systemd/system") / ESP_GPIO_LINK_SERVICE_NAME
ESP_GPIO_LINK_CONFIG_PATH = Path("/etc/mido-wifi-portal/esp-gpio-link.json")
ESP_GPIO_LINK_STATE_PATH = Path("/run/mido-esp-gpio-link-state.json")
ESP_LINK_PROTOCOL_MAGIC = 0x0A
ESP_LINK_PROTOCOL_VERSION = 1
ESP_GPIO_LINK_DEFAULT = {
    "enabled": True,
    "chip": "/dev/gpiochip0",
    "line": 64,
    "idle_high": True,
    "sync_ms": 48.0,
    "symbol_base_ms": 6.0,
    "symbol_step_ms": 2.0,
    "symbol_gap_ms": 6.0,
    "frame_interval": 0.5,
    "start_temp": 42.0,
    "full_temp": 70.0,
    "min_duty": 40.0,
    "max_duty": 100.0,
    "fan_active_low": False,
    "led_duty": 10.0,
    "led_active_low": True,
    "led_sleep_enabled": False,
    "led_sleep_after_minutes": 5.0,
    "blink_on_disconnect": True,
    "blink_interval": 0.8,
    "blink_duty": 25.0,
    "esp_api_url": "http://192.168.2.25/api/io",
    "log_interval": 30.0,
    "manual_timeout_seconds": 120.0,
    "manual_until": 0.0,
    "manual_led_enabled": False,
    "manual_led_duty": 0.0,
    "manual_fan_enabled": False,
    "manual_fan_duty": 0.0,
}
OPENCLASH_CONFIG_CANDIDATES = [
    Path("/etc/openclash/config.yaml"),
    Path("/etc/mihomo/config.yaml"),
    Path("/etc/clash/config.yaml"),
]
OPENCLASH_SUBSCRIPTION_DB = Path("/etc/mido-wifi-portal/openclash-subscriptions.json")
OPENCLASH_SUBSCRIPTION_DIR = Path("/etc/mihomo/subscriptions")


def json_response(handler, payload, status=HTTPStatus.OK):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
    handler.send_header("Pragma", "no-cache")
    handler.send_header("Expires", "0")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def set_message(msg):
    with STATE_LOCK:
        STATE["last_message"] = msg


def active_connections():
    try:
        proc = nmcli("-t", "-f", "NAME,UUID,TYPE,DEVICE", "connection", "show", "--active")
    except subprocess.CalledProcessError:
        return []

    rows = []
    for line in proc.stdout.splitlines():
        parts = line.rsplit(":", 3)
        if len(parts) == 4:
            rows.append({"name": parts[0], "uuid": parts[1], "type": parts[2], "device": parts[3]})
    return rows


def load_saved_wifi_metadata():
    profiles = {}
    base_dir = Path("/etc/NetworkManager/system-connections")
    if not base_dir.exists():
        return profiles

    for path in base_dir.glob("*.nmconnection"):
        parser = configparser.ConfigParser(interpolation=None)
        try:
            with path.open("r", encoding="utf-8") as fp:
                parser.read_file(fp)
        except Exception:
            continue

        uuid = parser.get("connection", "uuid", fallback="").strip()
        if not uuid:
            continue

        profiles[uuid] = {
            "profile_name": parser.get("connection", "id", fallback="").strip(),
            "connection_type": parser.get("connection", "type", fallback="").strip(),
            "ssid": parser.get("wifi", "ssid", fallback="").strip(),
            "filename": path.name,
        }

    return profiles


def hotspot_is_active():
    hotspot_name = CONFIG["hotspot_connection"]
    return any(row["name"] == hotspot_name for row in active_connections())


def ensure_hotspot_profile():
    hotspot_name = CONFIG["hotspot_connection"]
    iface = CONFIG["wifi_interface"]

    existing = nmcli("-t", "-f", "NAME", "connection", "show", check=False).stdout.splitlines()
    if hotspot_name not in existing:
        nmcli(
            "connection",
            "add",
            "type",
            "wifi",
            "ifname",
            iface,
            "con-name",
            hotspot_name,
            "autoconnect",
            "no",
            "ssid",
            CONFIG["hotspot_ssid"],
        )

    nmcli(
        "connection",
        "modify",
        hotspot_name,
        "connection.autoconnect",
        "no",
        "802-11-wireless.mode",
        "ap",
        "802-11-wireless.band",
        "bg",
        "ipv4.method",
        "shared",
        "ipv6.method",
        "ignore",
        "802-11-wireless-security.key-mgmt",
        "wpa-psk",
        "802-11-wireless-security.psk",
        CONFIG["hotspot_password"],
        "wifi-sec.group",
        "ccmp",
        "wifi-sec.pairwise",
        "ccmp",
    )


def start_hotspot():
    ensure_hotspot_profile()
    scan_networks(allow_cached_only=False)
    nmcli("connection", "up", CONFIG["hotspot_connection"])
    with STATE_LOCK:
        STATE["hotspot_active"] = True
    set_message(f"热点已启动：{CONFIG['hotspot_ssid']}")


def stop_hotspot():
    nmcli("connection", "down", CONFIG["hotspot_connection"], check=False)
    with STATE_LOCK:
        STATE["hotspot_active"] = False
    set_message("热点已关闭")


def has_uplink():
    connectivity = connectivity_state()
    if connectivity in {"full", "limited", "portal"}:
        return True

    route = run(["ip", "route", "show", "default"], check=False)
    if not route.stdout.strip():
        return False

    probe = run(["ping", "-4", "-c", "1", "-W", "2", CONFIG["check_host"]], check=False)
    return probe.returncode == 0


def ip4_address():
    proc = nmcli("-g", "IP4.ADDRESS", "device", "show", CONFIG["wifi_interface"], check=False)
    values = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    if values:
        return values[0]
    return ""


def dns_servers():
    servers = []
    proc = nmcli("-g", "IP4.DNS", "device", "show", CONFIG["wifi_interface"], check=False)
    for line in proc.stdout.splitlines():
        value = line.strip()
        if value and value not in servers:
            servers.append(value)

    if not servers:
        try:
            for line in Path("/etc/resolv.conf").read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if not line.startswith("nameserver "):
                    continue
                value = line.split(None, 1)[1].strip()
                if value and value not in servers:
                    servers.append(value)
        except Exception:
            pass

    return servers


def dns_servers_fast():
    servers = []
    try:
        for line in Path("/etc/resolv.conf").read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line.startswith("nameserver "):
                continue
            value = line.split(None, 1)[1].strip()
            if value and value not in servers:
                servers.append(value)
    except Exception:
        pass
    return servers


def ip4_address_fast():
    proc = run(["ip", "-4", "-o", "addr", "show", "dev", CONFIG["wifi_interface"], "scope", "global"], check=False)
    for line in proc.stdout.splitlines():
        parts = line.split()
        if "inet" not in parts:
            continue
        idx = parts.index("inet")
        if idx + 1 < len(parts):
            return parts[idx + 1].split("/", 1)[0]
    return ""


def interface_counters(interface):
    stats_dir = Path("/sys/class/net") / interface / "statistics"
    try:
        rx_bytes = int((stats_dir / "rx_bytes").read_text(encoding="utf-8").strip())
        tx_bytes = int((stats_dir / "tx_bytes").read_text(encoding="utf-8").strip())
    except Exception:
        rx_bytes = 0
        tx_bytes = 0
    return {"rx_bytes": rx_bytes, "tx_bytes": tx_bytes}


def device_status():
    proc = nmcli("-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device", "status", check=False)
    rows = []
    for line in proc.stdout.splitlines():
        parts = line.split(":", 3)
        if len(parts) == 4:
            rows.append(
                {
                    "device": parts[0],
                    "type": parts[1],
                    "state": parts[2],
                    "connection": parts[3],
                }
            )
    return rows


def saved_wifi_connections():
    proc = nmcli("-t", "-f", "NAME,UUID,TYPE,AUTOCONNECT,DEVICE", "connection", "show", check=False)
    active_uuids = {row["uuid"] for row in active_connections() if row.get("uuid")}
    metadata = load_saved_wifi_metadata()
    rows = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.rsplit(":", 4)
        if len(parts) != 5:
            continue
        name, uuid, conn_type, autoconnect, device = parts
        if conn_type not in {"802-11-wireless", "wifi"}:
            continue
        if name == CONFIG["hotspot_connection"]:
            continue
        meta = metadata.get(uuid, {})
        display_name = meta.get("ssid") or name
        rows.append(
            {
                "name": display_name,
                "ssid": meta.get("ssid", ""),
                "profile_name": meta.get("profile_name") or name,
                "uuid": uuid,
                "autoconnect": autoconnect,
                "device": device,
                "active": uuid in active_uuids,
                "filename": meta.get("filename", ""),
            }
        )

    rows.sort(key=lambda item: (not item["active"], item["name"].lower()))
    return rows


def saved_wifi_count_fast():
    count = 0
    for item in load_saved_wifi_metadata().values():
        if item.get("connection_type") not in {"wifi", "802-11-wireless"}:
            continue
        if item.get("profile_name") == CONFIG["hotspot_connection"]:
            continue
        count += 1
    return count


def activate_wifi_connection(uuid):
    if not uuid:
        raise ValueError("缺少 Wi-Fi UUID")

    with STATE_LOCK:
        STATE["suppress_hotspot_until"] = time.time() + float(CONFIG["reconnect_grace_seconds"])

    stop_hotspot()
    proc = nmcli("connection", "up", "uuid", uuid, check=False)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "连接失败")

    set_message("已请求连接已保存的 Wi-Fi")
    return proc.stdout.strip() or "OK"


def delete_wifi_connection(uuid):
    if not uuid:
        raise ValueError("缺少 Wi-Fi UUID")

    proc = nmcli("connection", "delete", "uuid", uuid, check=False)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "删除失败")

    set_message("已删除 Wi-Fi 配置")
    return proc.stdout.strip() or "OK"


def scan_networks(allow_cached_only=True):
    if hotspot_is_active() and allow_cached_only:
        with STATE_LOCK:
            return {
                "networks": list(STATE["last_scan"]),
                "cached": True,
                "updated_at": STATE["last_scan_at"],
                "message": "热点运行中，显示上次扫描结果",
            }

    proc = nmcli(
        "-m",
        "multiline",
        "-c",
        "no",
        "-f",
        "IN-USE,SSID,SIGNAL,SECURITY,BSSID",
        "device",
        "wifi",
        "list",
        "ifname",
        CONFIG["wifi_interface"],
        "--rescan",
        "yes",
        check=False,
    )

    networks = []
    current = {}
    for raw_line in proc.stdout.splitlines():
        line = raw_line.strip()
        if not line:
            if current:
                networks.append(current)
                current = {}
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        current[key.strip().lower()] = value.strip()
    if current:
        networks.append(current)

    normalized = []
    for row in networks:
        normalized.append(
            {
                "in_use": row.get("in-use", "") == "*",
                "ssid": row.get("ssid", ""),
                "signal": row.get("signal", ""),
                "security": row.get("security", ""),
                "bssid": row.get("bssid", ""),
            }
        )

    with STATE_LOCK:
        STATE["last_scan"] = normalized
        STATE["last_scan_at"] = time.time()

    return {"networks": normalized, "cached": False, "updated_at": STATE["last_scan_at"], "message": ""}


def connect_wifi(ssid, password, hidden=False):
    if not ssid:
        raise ValueError("SSID 不能为空")

    with STATE_LOCK:
        STATE["suppress_hotspot_until"] = time.time() + float(CONFIG["reconnect_grace_seconds"])

    stop_hotspot()
    nmcli("connection", "delete", ssid, check=False)

    cmd = [
        "device",
        "wifi",
        "connect",
        ssid,
        "ifname",
        CONFIG["wifi_interface"],
        "name",
        ssid,
    ]
    if password:
        cmd.extend(["password", password])
    if hidden:
        cmd.extend(["hidden", "yes"])

    proc = nmcli(*cmd, check=False)
    if proc.returncode != 0:
        set_message(proc.stderr.strip() or proc.stdout.strip() or "连接失败")
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "连接失败")

    set_message(f"已提交连接：{ssid}")
    return proc.stdout.strip() or "OK"


def format_bytes(num):
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(num)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def format_duration(seconds):
    total = int(max(seconds, 0))
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days:
        return f"{days}天 {hours}小时"
    if hours:
        return f"{hours}小时 {minutes}分钟"
    return f"{minutes}分钟"


def cpu_usage_percent():
    def snapshot():
        values = [int(item) for item in Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0].split()[1:]]
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        total = sum(values)
        return idle, total

    try:
        idle1, total1 = snapshot()
        time.sleep(0.12)
        idle2, total2 = snapshot()
        total_delta = total2 - total1
        idle_delta = idle2 - idle1
        if total_delta <= 0:
            return 0.0
        return max(0.0, min(100.0, (1.0 - idle_delta / total_delta) * 100.0))
    except Exception:
        return 0.0


def cpu_core_usage():
    def snapshot():
        rows = {}
        for line in Path("/proc/stat").read_text(encoding="utf-8").splitlines():
            if not re.match(r"^cpu\d+\s", line):
                continue
            name = line.split()[0]
            values = [int(item) for item in line.split()[1:]]
            idle = values[3] + (values[4] if len(values) > 4 else 0)
            total = sum(values)
            rows[name] = (idle, total)
        return rows

    try:
        first = snapshot()
        time.sleep(0.12)
        second = snapshot()
        result = []
        for name, (idle2, total2) in sorted(second.items()):
            idle1, total1 = first.get(name, (idle2, total2))
            total_delta = total2 - total1
            idle_delta = idle2 - idle1
            if total_delta <= 0:
                percent = 0.0
            else:
                percent = max(0.0, min(100.0, (1.0 - idle_delta / total_delta) * 100.0))
            result.append({"name": name, "percent": round(percent, 1), "text": f"{percent:.1f}%"})
        return result
    except Exception:
        return []


def temperature_sensors():
    sensors = []
    for path in sorted(Path("/sys/class/thermal").glob("thermal_zone*")):
        temp_path = path / "temp"
        if not temp_path.exists():
            continue
        try:
            raw = float(temp_path.read_text(encoding="utf-8").strip())
            celsius = raw / 1000.0 if raw > 1000 else raw
            if celsius < -50 or celsius > 200:
                continue
            sensor_type = (path / "type").read_text(encoding="utf-8").strip() if (path / "type").exists() else path.name
            sensors.append({"name": sensor_type or path.name, "celsius": round(celsius, 1), "text": f"{celsius:.1f}°C"})
        except Exception:
            continue
    return sensors[:8]


def system_summary():
    uptime_seconds = 0.0
    try:
        uptime_seconds = float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0])
    except Exception:
        pass

    mem_total = 0
    mem_available = 0
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemTotal:"):
                mem_total = int(line.split()[1]) * 1024
            elif line.startswith("MemAvailable:"):
                mem_available = int(line.split()[1]) * 1024
    except Exception:
        pass

    disk_total, disk_used, disk_free = shutil.disk_usage("/")
    try:
        load1, load5, load15 = os.getloadavg()
    except OSError:
        load1 = load5 = load15 = 0.0

    cpu_percent = round(cpu_usage_percent(), 1)
    cpu_cores = cpu_core_usage()
    temperatures = temperature_sensors()

    return {
        "uptime": format_duration(uptime_seconds),
        "load": f"{load1:.2f} / {load5:.2f} / {load15:.2f}",
        "cpu_percent": cpu_percent,
        "cpu_percent_text": f"{cpu_percent:.1f}%",
        "cpu_cores": cpu_cores,
        "temperatures": temperatures,
        "memory_used": format_bytes(max(mem_total - mem_available, 0)),
        "memory_total": format_bytes(mem_total),
        "memory_used_bytes": max(mem_total - mem_available, 0),
        "memory_total_bytes": mem_total,
        "disk_used": format_bytes(disk_used),
        "disk_total": format_bytes(disk_total),
        "disk_used_bytes": disk_used,
        "disk_total_bytes": disk_total,
        "disk_free": format_bytes(disk_free),
    }


def run_console(command):
    if not command or not command.strip():
        raise ValueError("命令不能为空")

    try:
        proc = subprocess.run(
            ["bash", "-lc", command],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or ""
        error = exc.stderr or ""
        merged = output + ("\n" if output and error else "") + error
        return {"exit_code": -1, "output": merged.strip(), "timeout": True}

    merged = proc.stdout
    if proc.stderr:
        if merged and not merged.endswith("\n"):
            merged += "\n"
        merged += proc.stderr

    merged = merged.strip()
    if len(merged) > 20000:
        merged = merged[:20000] + "\n...[output truncated]..."

    return {"exit_code": proc.returncode, "output": merged, "timeout": False}


def strip_ansi(text):
    clean = ANSI_ESCAPE_RE.sub("", text or "")
    return clean.replace("\r", "")


def build_slow_status_snapshot():
    return {
        "ip4": ip4_address(),
        "dns_servers": dns_servers(),
        "connectivity": connectivity_state(),
        "uplink": has_uplink(),
        "hotspot_active": hotspot_is_active(),
        "saved_wifi_count": len(saved_wifi_connections()),
        "device_status": device_status(),
        "active_connections": active_connections(),
        "services": collect_service_health(),
    }


def request_status_refresh(force=False):
    now = time.time()
    with STATUS_CACHE_LOCK:
        if STATUS_CACHE["refreshing"]:
            return False
        if not force and STATUS_CACHE["slow_data"] and now - STATUS_CACHE["updated_at"] < STATUS_CACHE_TTL:
            return False
        STATUS_CACHE["refreshing"] = True

    def worker():
        error = ""
        data = None
        try:
            data = build_slow_status_snapshot()
        except Exception as exc:
            error = str(exc)
        finally:
            with STATUS_CACHE_LOCK:
                if data is not None:
                    STATUS_CACHE["slow_data"] = data
                    STATUS_CACHE["updated_at"] = time.time()
                STATUS_CACHE["last_error"] = error
                STATUS_CACHE["refreshing"] = False

    threading.Thread(target=worker, daemon=True).start()
    return True


def cleanup_terminal_sessions():
    now = time.time()
    stale_ids = []
    with TERMINAL_LOCK:
        for session_id, session in TERMINAL_SESSIONS.items():
            proc = session["proc"]
            if session["closed"] or proc.poll() is not None or now - session["last_active"] > TERMINAL_IDLE_TIMEOUT:
                stale_ids.append(session_id)

    for session_id in stale_ids:
        close_terminal_session(session_id)


def create_terminal_session():
    cleanup_terminal_sessions()
    master_fd, slave_fd = pty.openpty()
    env = os.environ.copy()
    env["HOME"] = "/root"
    env["TERM"] = "dumb"
    env["PS1"] = "root@mido:\\w# "

    def terminal_preexec():
        os.setsid()
        fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)

    proc = subprocess.Popen(
        ["bash", "--noprofile", "--norc", "-i"],
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        cwd="/root",
        env=env,
        close_fds=True,
        preexec_fn=terminal_preexec,
    )
    os.close(slave_fd)

    session_id = uuid.uuid4().hex
    session = {
        "id": session_id,
        "proc": proc,
        "master": master_fd,
        "buffer": bytearray(),
        "base_offset": 0,
        "closed": False,
        "last_active": time.time(),
    }

    with TERMINAL_LOCK:
        TERMINAL_SESSIONS[session_id] = session

    def reader():
        while True:
            try:
                chunk = os.read(master_fd, 4096)
            except OSError:
                chunk = b""
            if not chunk:
                break

            with TERMINAL_LOCK:
                current = TERMINAL_SESSIONS.get(session_id)
                if not current:
                    break
                current["buffer"].extend(chunk)
                overflow = len(current["buffer"]) - TERMINAL_MAX_BUFFER
                if overflow > 0:
                    del current["buffer"][:overflow]
                    current["base_offset"] += overflow
                current["last_active"] = time.time()

        with TERMINAL_LOCK:
            current = TERMINAL_SESSIONS.get(session_id)
            if current:
                current["closed"] = True
                current["last_active"] = time.time()

    threading.Thread(target=reader, daemon=True).start()
    time.sleep(0.12)
    result = terminal_output(session_id, 0)
    result["session_id"] = session_id
    return result


def terminal_output(session_id, cursor=0):
    if not session_id:
        raise ValueError("缺少终端会话 ID")

    cleanup_terminal_sessions()
    with TERMINAL_LOCK:
        session = TERMINAL_SESSIONS.get(session_id)
        if not session:
            raise RuntimeError("终端会话不存在或已失效")

        base_offset = session["base_offset"]
        safe_cursor = max(int(cursor), base_offset)
        start = max(0, safe_cursor - base_offset)
        payload = bytes(session["buffer"][start:])
        next_cursor = base_offset + len(session["buffer"])
        exit_code = session["proc"].poll()
        closed = session["closed"] or exit_code is not None
        session["last_active"] = time.time()

    return {
        "session_id": session_id,
        "cursor": next_cursor,
        "output": strip_ansi(payload.decode("utf-8", "replace")),
        "closed": closed,
        "exit_code": exit_code,
    }


def terminal_input(session_id, data):
    if not session_id:
        raise ValueError("缺少终端会话 ID")

    text = "" if data is None else str(data)
    if text == "":
        return {"written": 0}

    cleanup_terminal_sessions()
    with TERMINAL_LOCK:
        session = TERMINAL_SESSIONS.get(session_id)
        if not session:
            raise RuntimeError("终端会话不存在或已失效")
        master_fd = session["master"]
        session["last_active"] = time.time()

    written = os.write(master_fd, text.encode("utf-8"))
    return {"written": written}


def close_terminal_session(session_id):
    if not session_id:
        return {"closed": False}

    with TERMINAL_LOCK:
        session = TERMINAL_SESSIONS.pop(session_id, None)
    if not session:
        return {"closed": False}

    session["closed"] = True
    proc = session["proc"]
    master_fd = session["master"]
    try:
        os.write(master_fd, b"exit\n")
    except OSError:
        pass

    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=1.5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=1.5)

    try:
        os.close(master_fd)
    except OSError:
        pass

    return {"closed": True}


def openclash_candidates():
    return [
        {"name": "openclash", "service": "openclash.service", "command": "openclash"},
        {"name": "mihomo", "service": "mihomo.service", "command": "mihomo"},
        {"name": "clash-meta", "service": "clash-meta.service", "command": "clash-meta"},
        {"name": "clash", "service": "clash.service", "command": "clash"},
    ]


def detect_openclash():
    for candidate in openclash_candidates():
        service_state = run(["systemctl", "is-enabled", candidate["service"]], check=False)
        active_state = run(["systemctl", "is-active", candidate["service"]], check=False)
        command_exists = shutil.which(candidate["command"]) is not None
        if command_exists or service_state.returncode == 0 or active_state.returncode == 0:
            return {
                **candidate,
                "active": active_state.stdout.strip() == "active",
                "enabled": service_state.stdout.strip() not in {"", "disabled", "not-found"},
            }
    return None


def openclash_status():
    detected = detect_openclash()
    if not detected:
        return {
            "installed": False,
            "name": "",
            "service": "",
            "active": False,
            "enabled": False,
            "ports": [],
            "message": "未检测到 OpenClash / Mihomo / Clash 服务",
        }

    ports = []
    proc = run(["ss", "-ltn"], check=False)
    for line in proc.stdout.splitlines():
        fields = line.split()
        if len(fields) < 4:
            continue
        local = fields[3]
        if ":" not in local:
            continue
        port = local.rsplit(":", 1)[-1]
        if port in {"7890", "7891", "7892", "9090"}:
            ports.append({"port": port, "listen": local})

    return {
        "installed": True,
        "name": detected["name"],
        "service": detected["service"],
        "active": detected["active"],
        "enabled": detected["enabled"],
        "ports": ports,
        "message": "已检测到代理服务",
    }


def openclash_action(action):
    detected = detect_openclash()
    if not detected:
        raise RuntimeError("当前系统未检测到 OpenClash / Mihomo / Clash 服务")

    if action not in {"start", "stop", "restart"}:
        raise ValueError("未知动作")

    proc = run(["systemctl", action, detected["service"]], check=False)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "服务操作失败")

    set_message(f"{detected['service']} 已执行 {action}")
    return proc.stdout.strip() or f"{detected['service']} {action} OK"


def openclash_config_path():
    for path in OPENCLASH_CONFIG_CANDIDATES:
        if path.exists():
            return path
    return OPENCLASH_CONFIG_CANDIDATES[1]


def read_openclash_config():
    path = openclash_config_path()
    content = ""
    if path.exists():
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            content = path.read_text(encoding="utf-8", errors="replace")
    return {"path": str(path), "content": content}


def save_openclash_config(content, restart=False):
    path = openclash_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.replace("\r\n", "\n"), encoding="utf-8")
    set_message(f"已保存配置：{path}")
    if restart:
        openclash_action("restart")
    return str(path)


def openclash_controller_settings():
    content = read_openclash_config().get("content", "")
    port = "9090"
    secret = ""
    controller_match = re.search(r"^\s*external-controller:\s*['\"]?([^\r\n'\"]+)['\"]?\s*$", content, re.MULTILINE)
    if controller_match:
        raw = controller_match.group(1).strip()
        if ":" in raw:
            port = raw.rsplit(":", 1)[-1].strip() or port
    secret_match = re.search(r"^\s*secret:\s*['\"]?([^\r\n'\"]+)['\"]?\s*$", content, re.MULTILINE)
    if secret_match:
        secret = secret_match.group(1).strip()
    return {"base_url": f"http://127.0.0.1:{port}", "secret": secret}


def openclash_controller_request(path, method="GET", payload=None):
    settings = openclash_controller_settings()
    headers = {}
    if settings["secret"]:
        headers["Authorization"] = f"Bearer {settings['secret']}"
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = Request(f"{settings['base_url']}{path}", data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=8) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return json.loads(body) if body else {}
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(detail or f"控制器请求失败: {exc.code}") from exc
    except URLError as exc:
        raise RuntimeError(f"控制器不可达: {exc.reason}") from exc


def openclash_proxy_groups():
    data = openclash_controller_request("/proxies")
    groups = []
    for name, item in data.get("proxies", {}).items():
        choices = item.get("all") or []
        if not choices:
            continue
        groups.append(
            {
                "name": name,
                "type": item.get("type", ""),
                "current": item.get("now", ""),
                "options": choices,
            }
        )
    groups.sort(key=lambda entry: entry["name"].lower())
    return {"groups": groups}


def openclash_switch_proxy(group, name):
    if not group or not name:
        raise ValueError("缺少策略组或节点名")
    openclash_controller_request(f"/proxies/{quote(group, safe='')}", method="PUT", payload={"name": name})
    set_message(f"策略组 {group} 已切换到 {name}")
    return f"策略组 {group} 已切换到 {name}"


def subscription_store():
    OPENCLASH_SUBSCRIPTION_DB.parent.mkdir(parents=True, exist_ok=True)
    if not OPENCLASH_SUBSCRIPTION_DB.exists():
        return {"active": "", "items": []}
    try:
        with OPENCLASH_SUBSCRIPTION_DB.open("r", encoding="utf-8") as fp:
            data = json.load(fp)
    except Exception:
        return {"active": "", "items": []}
    data.setdefault("active", "")
    data.setdefault("items", [])
    return data


def save_subscription_store(data):
    OPENCLASH_SUBSCRIPTION_DB.parent.mkdir(parents=True, exist_ok=True)
    with OPENCLASH_SUBSCRIPTION_DB.open("w", encoding="utf-8") as fp:
        json.dump(data, fp, ensure_ascii=False, indent=2)


def subscription_slug(name):
    slug = re.sub(r"[^0-9A-Za-z._-]+", "-", (name or "").strip()).strip("-._")
    if not slug:
        raise ValueError("订阅名称不能为空")
    return slug


def subscription_file_path(name):
    OPENCLASH_SUBSCRIPTION_DIR.mkdir(parents=True, exist_ok=True)
    return OPENCLASH_SUBSCRIPTION_DIR / f"{subscription_slug(name)}.yaml"


def subscription_index(data, name):
    for index, item in enumerate(data.get("items", [])):
        if item.get("name") == name:
            return index
    return -1


def download_subscription(url):
    if not url:
        raise ValueError("订阅地址不能为空")
    req = Request(url, headers={"User-Agent": "MidoPortal/1.0"})
    with urlopen(req, timeout=20) as resp:
        body = resp.read()
    text = body.decode("utf-8", errors="replace").replace("\r\n", "\n")
    if ":" not in text:
        raise RuntimeError("订阅内容不是有效的 Clash/Mihomo 配置")
    return text


def apply_subscription_file(source_path):
    target = openclash_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source_path, target)
    openclash_action("restart")
    return str(target)


def list_openclash_subscriptions():
    data = subscription_store()
    active = data.get("active", "")
    items = []
    for item in data.get("items", []):
        path = subscription_file_path(item.get("name", ""))
        items.append(
            {
                "name": item.get("name", ""),
                "url": item.get("url", ""),
                "updated_at": item.get("updated_at", 0),
                "path": str(path),
                "exists": path.exists(),
                "active": item.get("name", "") == active,
            }
        )
    return {"active": active, "items": items}


def upsert_openclash_subscription(name, url, activate=False):
    name = (name or "").strip()
    url = (url or "").strip()
    if not name:
        raise ValueError("订阅名称不能为空")
    if not url:
        raise ValueError("订阅地址不能为空")
    content = download_subscription(url)
    path = subscription_file_path(name)
    path.write_text(content, encoding="utf-8")

    data = subscription_store()
    entry = {"name": name, "url": url, "updated_at": int(time.time())}
    idx = subscription_index(data, name)
    if idx >= 0:
        data["items"][idx] = entry
    else:
        data["items"].append(entry)
    if activate:
        apply_subscription_file(path)
        data["active"] = name
    save_subscription_store(data)
    set_message(f"订阅 {name} 已保存")
    return {"name": name, "path": str(path), "active": activate}


def refresh_openclash_subscription(name, activate=False):
    data = subscription_store()
    idx = subscription_index(data, (name or "").strip())
    if idx < 0:
        raise RuntimeError("订阅不存在")
    item = data["items"][idx]
    result = upsert_openclash_subscription(item["name"], item["url"], activate=activate or data.get("active") == item["name"])
    if activate:
        data = subscription_store()
        data["active"] = item["name"]
        save_subscription_store(data)
    return result


def activate_openclash_subscription(name):
    name = (name or "").strip()
    data = subscription_store()
    idx = subscription_index(data, name)
    if idx < 0:
        raise RuntimeError("订阅不存在")
    path = subscription_file_path(name)
    if not path.exists():
        raise RuntimeError("订阅文件不存在，请先刷新订阅")
    target = apply_subscription_file(path)
    data["active"] = name
    save_subscription_store(data)
    set_message(f"订阅 {name} 已启用")
    return {"name": name, "path": target}


def delete_openclash_subscription(name):
    name = (name or "").strip()
    data = subscription_store()
    idx = subscription_index(data, name)
    if idx < 0:
        raise RuntimeError("订阅不存在")
    path = subscription_file_path(name)
    data["items"].pop(idx)
    if data.get("active") == name:
        data["active"] = ""
    save_subscription_store(data)
    if path.exists():
        path.unlink()
    set_message(f"订阅 {name} 已删除")
    return {"name": name, "deleted": True}


def systemd_service_summary(service_name, label, detail=None):
    active_proc = run(["systemctl", "is-active", service_name], check=False)
    enabled_proc = run(["systemctl", "is-enabled", service_name], check=False)
    state = active_proc.stdout.strip() or "inactive"
    enabled_state = enabled_proc.stdout.strip()
    enabled = enabled_state not in {"", "disabled", "not-found", "masked"}
    return {
        "label": label,
        "name": service_name,
        "active": state == "active",
        "enabled": enabled,
        "state": state,
        "detail": detail or service_name,
    }


def docker_container_summary(name, label):
    if shutil.which("docker") is None:
        return {
            "label": label,
            "name": name,
            "active": False,
            "enabled": None,
            "state": "missing-docker",
            "detail": "docker 不可用",
        }

    proc = run(
        ["docker", "inspect", "-f", "{{.State.Status}}|{{.Config.Image}}|{{.Name}}", name],
        check=False,
    )
    if proc.returncode != 0:
        return {
            "label": label,
            "name": name,
            "active": False,
            "enabled": None,
            "state": "missing",
            "detail": "容器不存在",
        }

    raw = proc.stdout.strip()
    status, image, container_name = (raw.split("|", 2) + ["", "", ""])[:3]
    tag = image.rsplit(":", 1)[-1] if ":" in image else image
    detail = tag
    if container_name:
        detail = f"{container_name.lstrip('/')} · {tag}"

    return {
        "label": label,
        "name": name,
        "active": status == "running",
        "enabled": None,
        "state": status,
        "detail": detail,
    }


def docker_available():
    return shutil.which("docker") is not None


def docker_state_from_status(status):
    value = (status or "").strip().lower()
    if value.startswith("up "):
        return "running"
    if value.startswith("restarting"):
        return "restarting"
    if value.startswith("exited"):
        return "exited"
    if value.startswith("created"):
        return "created"
    if value.startswith("paused"):
        return "paused"
    if value.startswith("removing"):
        return "removing"
    if value.startswith("dead"):
        return "dead"
    return value or "unknown"


def list_docker_containers():
    service = systemd_service_summary("docker.service", "Docker")
    if not docker_available():
        return {
            "available": False,
            "service": service,
            "running": 0,
            "total": 0,
            "containers": [],
            "message": "系统未安装 Docker",
        }

    proc = run(["docker", "ps", "-a", "--format", "{{.Names}}|{{.Image}}|{{.Status}}"], check=False)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "读取 Docker 容器失败")

    rows = []
    running = 0
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        name, image, status = (line.split("|", 2) + ["", "", ""])[:3]
        state = docker_state_from_status(status)
        active = state == "running"
        if active:
            running += 1
        rows.append(
            {
                "name": name,
                "image": image,
                "image_tag": image.rsplit(":", 1)[-1] if ":" in image else image,
                "status": status,
                "state": state,
                "active": active,
            }
        )

    rows.sort(key=lambda item: (not item["active"], item["name"].lower()))
    return {
        "available": True,
        "service": service,
        "running": running,
        "total": len(rows),
        "containers": rows,
        "message": "Docker 容器列表已更新",
    }


def docker_container_action(name, action):
    name = (name or "").strip()
    if not name:
        raise ValueError("缺少容器名")
    if action not in {"start", "stop", "restart"}:
        raise ValueError("未知 Docker 动作")
    if not docker_available():
        raise RuntimeError("系统未安装 Docker")

    proc = run(["docker", action, name], check=False)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "Docker 操作失败")

    set_message(f"容器 {name} 已执行 {action}")
    return proc.stdout.strip() or f"{name} {action} OK"


def docker_container_logs(name, tail=120):
    name = (name or "").strip()
    if not name:
        raise ValueError("缺少容器名")
    if not docker_available():
        raise RuntimeError("系统未安装 Docker")

    try:
        proc = subprocess.run(
            ["docker", "logs", "--tail", str(max(1, int(tail))), name],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") + ("\n" if exc.stdout and exc.stderr else "") + (exc.stderr or "")
        merged = output.strip()
        if len(merged) > 20000:
            merged = merged[:20000] + "\n...[output truncated]..."
        return {"name": name, "output": merged, "timeout": True, "exit_code": -1}

    merged = proc.stdout
    if proc.stderr:
        if merged and not merged.endswith("\n"):
            merged += "\n"
        merged += proc.stderr
    merged = merged.strip()
    if len(merged) > 20000:
        merged = merged[:20000] + "\n...[output truncated]..."
    return {"name": name, "output": merged, "timeout": False, "exit_code": proc.returncode}


def detect_ssh_service_name():
    for candidate in ("ssh.service", "sshd.service"):
        state = run(["systemctl", "is-enabled", candidate], check=False)
        active = run(["systemctl", "is-active", candidate], check=False)
        if state.returncode == 0 or active.returncode == 0:
            return candidate
    return "ssh.service"


def collect_service_health():
    detected_proxy = detect_openclash()
    if detected_proxy:
        proxy = {
            "label": "代理",
            "name": detected_proxy["service"],
            "active": detected_proxy["active"],
            "enabled": detected_proxy["enabled"],
            "state": "active" if detected_proxy["active"] else "inactive",
            "detail": detected_proxy["service"],
        }
    else:
        proxy = {
            "label": "代理",
            "name": "",
            "active": False,
            "enabled": False,
            "state": "missing",
            "detail": "未安装 OpenClash / Mihomo",
        }

    ssh_service = detect_ssh_service_name()
    return {
        "proxy": proxy,
        "ssh": systemd_service_summary(ssh_service, "OpenSSH"),
        "docker": systemd_service_summary("docker.service", "Docker"),
        "supervisor": systemd_service_summary("hassio-supervisor.service", "Supervisor"),
        "agent": systemd_service_summary("haos-agent.service", "OS Agent"),
        "portal": systemd_service_summary("mido-wifi-portal.service", "Portal"),
        "homeassistant": docker_container_summary("homeassistant", "Home Assistant"),
    }


def service_action(service_key, action):
    if action not in {"start", "stop", "restart"}:
        raise ValueError("未知动作")

    service_map = {
        "ssh": detect_ssh_service_name(),
        "docker": "docker.service",
        "supervisor": "hassio-supervisor.service",
        "agent": "haos-agent.service",
        "portal": "mido-wifi-portal.service",
        "proxy": detect_openclash()["service"] if detect_openclash() else None,
    }

    service_name = service_map.get(service_key)
    if not service_name:
        raise RuntimeError("当前服务不可控制")

    proc = run(["systemctl", action, service_name], check=False)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "服务操作失败")

    set_message(f"{service_name} 已执行 {action}")
    return proc.stdout.strip() or f"{service_name} {action} OK"


def gpio_tools_ready():
    required = ("gpiodetect", "gpioinfo", "gpioget", "gpioset")
    return all(shutil.which(name) for name in required)


def gpio_key(chip, line):
    return f"{chip}:{int(line)}"


def systemctl_state(service, verb):
    proc = run(["systemctl", verb, service], check=False)
    return proc.returncode == 0, (proc.stdout.strip() or proc.stderr.strip())


def gpio_detect_chips():
    proc = run(["gpiodetect"], check=False)
    chips = []
    pattern = re.compile(r"^(gpiochip\d+)\s+\[(.*?)\]\s+\((\d+)\s+lines?\)$")
    for line in proc.stdout.splitlines():
        line = line.strip()
        match = pattern.match(line)
        if not match:
            continue
        chips.append(
            {
                "chip": match.group(1),
                "label": match.group(2),
                "lines": int(match.group(3)),
            }
        )
    return chips


def gpio_runtime_states():
    proc = run(["gpioinfo"], check=False)
    chip_pattern = re.compile(r"^(gpiochip\d+)\s*-\s*(\d+)\s+lines:\s*$")
    line_pattern = re.compile(r"^\s*line\s+(\d+):\s+(\S+)\s+(input|output)(.*)$")
    consumer_pattern = re.compile(r"\bconsumer=([^\s]+)")
    states = {}
    current_chip = ""

    for raw in proc.stdout.splitlines():
        line = raw.rstrip()
        chip_match = chip_pattern.match(line)
        if chip_match:
            current_chip = chip_match.group(1)
            continue
        if not current_chip:
            continue

        line_match = line_pattern.match(line)
        if not line_match:
            continue

        offset = int(line_match.group(1))
        direction = line_match.group(3)
        tail = line_match.group(4) or ""
        consumer_match = consumer_pattern.search(tail)
        consumer = consumer_match.group(1).strip() if consumer_match else ""
        states[(current_chip, offset)] = {
            "direction": direction,
            "consumer": consumer,
        }

    return states


def gpio_holds_snapshot():
    cleanup_gpio_holds()
    with GPIO_LOCK:
        return [
            {
                "chip": entry["chip"],
                "line": entry["line"],
                "value": entry["value"],
                "mode": entry.get("mode", "level"),
                "duty": entry.get("duty"),
                "hz": entry.get("hz"),
            }
            for entry in GPIO_HOLDS.values()
        ]


def gpio_allowed_lines(chips, holds):
    chip_map = {item["chip"]: item for item in chips}
    states = gpio_runtime_states()
    hold_keys = {(item["chip"], int(item["line"])) for item in holds}
    allowed = []
    blocked = []

    for item in SAFE_GPIO_LINES:
        chip = chip_map.get(item["chip"])
        if not chip:
            continue

        line = int(item["line"])
        if line >= int(chip["lines"]):
            continue

        state = states.get((item["chip"], line), {})
        consumer = state.get("consumer", "")
        payload = {
            **item,
            "line": line,
            "chip_label": chip["label"],
            "runtime_direction": state.get("direction", ""),
            "runtime_consumer": consumer,
        }

        if consumer and (item["chip"], line) not in hold_keys:
            blocked.append(payload)
            continue

        allowed.append(payload)

    return allowed, blocked


def gpio_allowed_entry(chip, line):
    chip = str(chip or "").strip()
    line = int(line)
    data = gpio_chips()
    for item in data.get("allowed_lines", []):
        if item["chip"] == chip and int(item["line"]) == line:
            return dict(item)
    raise ValueError(f"当前页面只允许操作当前真正空闲的 {len(data.get('allowed_lines', []))} 个 GPIO")


def parse_gpio_line(line):
    value = str(line or "").strip()
    if value == "":
        raise ValueError("请输入 GPIO line offset")
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError("GPIO line offset 必须是整数") from exc


def parse_gpio_duty(value):
    raw = str(value if value is not None else "").strip()
    if raw == "":
        raise ValueError("请输入占空比")
    try:
        duty = float(raw)
    except ValueError as exc:
        raise ValueError("占空比必须是数字") from exc
    if duty < 0 or duty > 100:
        raise ValueError("占空比必须在 0 到 100 之间")
    return round(duty, 2)


def parse_gpio_frequency(value):
    raw = str(value if value is not None else "").strip()
    if raw == "":
        raise ValueError("请输入 PWM 频率")
    try:
        hz = float(raw)
    except ValueError as exc:
        raise ValueError("PWM 频率必须是数字") from exc
    if hz < 1 or hz > 2000:
        raise ValueError("PWM 频率必须在 1 到 2000 Hz 之间")
    return round(hz, 2)


def parse_gpio_temperature(value, label):
    raw = str(value if value is not None else "").strip()
    if raw == "":
        raise ValueError(f"请输入{label}")
    try:
        temp = float(raw)
    except ValueError as exc:
        raise ValueError(f"{label}必须是数字") from exc
    if temp < 20 or temp > 120:
        raise ValueError(f"{label}必须在 20 到 120°C 之间")
    return round(temp, 1)


def parse_gpio_interval(value):
    raw = str(value if value is not None else "").strip()
    if raw == "":
        raise ValueError("请输入采样间隔")
    try:
        interval = float(raw)
    except ValueError as exc:
        raise ValueError("采样间隔必须是数字") from exc
    if interval < 0.2 or interval > 30:
        raise ValueError("采样间隔必须在 0.2 到 30 秒之间")
    return round(interval, 2)


def parse_number_range(value, label, minimum, maximum):
    raw = str(value if value is not None else "").strip()
    if raw == "":
        raise ValueError(f"请输入{label}")
    try:
        number = float(raw)
    except ValueError as exc:
        raise ValueError(f"{label}必须是数字") from exc
    if number < minimum or number > maximum:
        raise ValueError(f"{label}必须在 {minimum} 到 {maximum} 之间")
    return round(number, 2)


def clamp_percent(value):
    try:
        return max(0.0, min(100.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def logical_duty_from_signal(signal_duty, active_low):
    duty = clamp_percent(signal_duty)
    return round(100.0 - duty, 1) if active_low else round(duty, 1)


def _terminate_process(proc):
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=1.5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=1.5)


def cleanup_gpio_holds():
    dead_keys = []
    with GPIO_LOCK:
        for key, entry in GPIO_HOLDS.items():
            if entry["proc"].poll() is not None:
                dead_keys.append(key)
        for key in dead_keys:
            GPIO_HOLDS.pop(key, None)


def gpio_chips():
    if not gpio_tools_ready():
        return {"available": False, "chips": [], "allowed_lines": [], "blocked_lines": [], "holds": []}

    chips = gpio_detect_chips()
    holds = gpio_holds_snapshot()
    allowed_lines, blocked_lines = gpio_allowed_lines(chips, holds)

    chip_map = {item["chip"]: item for item in chips}
    chips = [chip_map[item["chip"]] for item in allowed_lines if item["chip"] in chip_map]
    dedup = {}
    for item in chips:
        dedup[item["chip"]] = item
    return {
        "available": True,
        "chips": list(dedup.values()),
        "allowed_lines": allowed_lines,
        "blocked_lines": blocked_lines,
        "holds": holds,
    }


def gpio_chip_limit(chip):
    data = gpio_chips()
    for item in data.get("chips", []):
        if item.get("chip") == chip:
            return int(item.get("lines", 0))
    raise ValueError("未找到对应的 gpiochip")


def gpio_read(chip, line):
    if not chip:
        raise ValueError("请选择 gpiochip")
    line = parse_gpio_line(line)
    gpio_allowed_entry(chip, line)
    limit = gpio_chip_limit(chip)
    if line < 0 or line >= limit:
        raise ValueError(f"{chip} 的有效范围是 0 到 {limit - 1}")
    cleanup_gpio_holds()
    key = gpio_key(chip, line)

    with GPIO_LOCK:
        entry = GPIO_HOLDS.get(key)
        if entry and entry["proc"].poll() is None:
            return {
                "chip": chip,
                "line": line,
                "value": entry["value"],
                "held": True,
                "mode": entry.get("mode", "level"),
                "duty": entry.get("duty"),
                "hz": entry.get("hz"),
            }

    proc = run(["gpioget", "-a", "--numeric", "-c", chip, str(line)], check=False)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "GPIO 读取失败")

    raw = proc.stdout.strip().splitlines()
    value = 0
    if raw:
        value = int(raw[-1].strip())
    return {"chip": chip, "line": line, "value": value, "held": False, "mode": "read", "duty": None, "hz": None}


def gpio_write(chip, line, value):
    if not chip:
        raise ValueError("请选择 gpiochip")
    line = parse_gpio_line(line)
    gpio_allowed_entry(chip, line)
    limit = gpio_chip_limit(chip)
    if line < 0 or line >= limit:
        raise ValueError(f"{chip} 的有效范围是 0 到 {limit - 1}")
    value = 1 if int(value) else 0
    cleanup_gpio_holds()
    key = gpio_key(chip, line)

    with GPIO_LOCK:
        existing = GPIO_HOLDS.pop(key, None)
    if existing:
        _terminate_process(existing["proc"])

    proc = subprocess.Popen(
        ["gpioset", "-c", chip, f"{line}={value}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(0.2)
    if proc.poll() is not None:
        error = proc.stderr.read().strip() if proc.stderr else ""
        raise RuntimeError(error or f"GPIO {chip}:{line} 设置失败")

    with GPIO_LOCK:
        GPIO_HOLDS[key] = {"proc": proc, "chip": chip, "line": line, "value": value, "mode": "level", "duty": None, "hz": None}

    set_message(f"GPIO {chip}:{line} 已保持为 {value}")
    return {"chip": chip, "line": line, "value": value, "held": True, "mode": "level", "duty": None, "hz": None}


def gpio_pwm(chip, line, duty, hz):
    if not chip:
        raise ValueError("请选择 gpiochip")
    line = parse_gpio_line(line)
    gpio_allowed_entry(chip, line)
    limit = gpio_chip_limit(chip)
    if line < 0 or line >= limit:
        raise ValueError(f"{chip} 的有效范围是 0 到 {limit - 1}")
    duty = parse_gpio_duty(duty)
    hz = parse_gpio_frequency(hz)
    cleanup_gpio_holds()
    key = gpio_key(chip, line)

    with GPIO_LOCK:
        existing = GPIO_HOLDS.pop(key, None)
    if existing:
        _terminate_process(existing["proc"])

    chip_path = chip if str(chip).startswith("/") else f"/dev/{chip}"
    proc = subprocess.Popen(
        [
            shutil.which("python3") or "/usr/bin/python3",
            str(GPIO_PWM_SCRIPT),
            "--chip",
            chip_path,
            "--line",
            str(line),
            "--duty",
            str(duty),
            "--hz",
            str(hz),
            "--consumer",
            f"mido-gpio-pwm-{line}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(0.25)
    if proc.poll() is not None:
        error = proc.stderr.read().strip() if proc.stderr else ""
        raise RuntimeError(error or f"GPIO {chip}:{line} PWM 设置失败")

    with GPIO_LOCK:
        GPIO_HOLDS[key] = {
            "proc": proc,
            "chip": chip,
            "line": line,
            "value": 1 if duty > 0 else 0,
            "mode": "pwm",
            "duty": duty,
            "hz": hz,
        }

    set_message(f"GPIO {chip}:{line} PWM 已设置为 {duty}% / {hz}Hz")
    return {"chip": chip, "line": line, "value": 1 if duty > 0 else 0, "held": True, "mode": "pwm", "duty": duty, "hz": hz}


def gpio_release(chip, line):
    if not chip:
        raise ValueError("请选择 gpiochip")
    line = parse_gpio_line(line)
    gpio_allowed_entry(chip, line)
    limit = gpio_chip_limit(chip)
    if line < 0 or line >= limit:
        raise ValueError(f"{chip} 的有效范围是 0 到 {limit - 1}")
    key = gpio_key(chip, line)
    with GPIO_LOCK:
        entry = GPIO_HOLDS.pop(key, None)
    if not entry:
        return {"chip": chip, "line": line, "released": False}

    _terminate_process(entry["proc"])
    set_message(f"GPIO {chip}:{line} 已释放")
    return {"chip": chip, "line": line, "released": True}


def boot_led_service_exec(status):
    parts = [
        "/usr/bin/python3",
        "/opt/mido-wifi-portal/gpio64_led_pwm.py",
        "--chip",
        str(status["chip"]),
        "--line",
        str(int(status["line"])),
        "--duty",
        str(status["duty"]),
        "--hz",
        str(status["hz"]),
        "--auto-off-seconds",
        str(status["auto_off_seconds"]),
    ]
    if status.get("active_low"):
        parts.append("--active-low")
    return " ".join(shlex.quote(item) for item in parts)


def boot_led_status():
    status = dict(BOOT_LED_DEFAULT)
    status.update(
        {
            "service": BOOT_LED_SERVICE_NAME,
            "exists": BOOT_LED_SERVICE_PATH.exists(),
            "enabled": False,
            "active": False,
        }
    )

    if BOOT_LED_SERVICE_PATH.exists():
        text = BOOT_LED_SERVICE_PATH.read_text(encoding="utf-8", errors="ignore")
        match = re.search(r"^ExecStart=(.+)$", text, re.MULTILINE)
        if match:
            args = shlex.split(match.group(1).strip())
            for index, token in enumerate(args):
                if token == "--chip" and index + 1 < len(args):
                    status["chip"] = args[index + 1]
                elif token == "--line" and index + 1 < len(args):
                    status["line"] = int(args[index + 1])
                elif token == "--duty" and index + 1 < len(args):
                    status["duty"] = float(args[index + 1])
                elif token == "--hz" and index + 1 < len(args):
                    status["hz"] = float(args[index + 1])
                elif token == "--auto-off-seconds" and index + 1 < len(args):
                    status["auto_off_seconds"] = float(args[index + 1])
                elif token == "--active-low":
                    status["active_low"] = True

    status["enabled"], _ = systemctl_state(BOOT_LED_SERVICE_NAME, "is-enabled")
    status["active"], _ = systemctl_state(BOOT_LED_SERVICE_NAME, "is-active")
    return status


def boot_led_update(duty, hz):
    status = boot_led_status()
    status["duty"] = parse_gpio_duty(duty)
    status["hz"] = parse_gpio_frequency(hz)

    service_text = "\n".join(
        [
            "[Unit]",
            "Description=Mido GPIO64 PWM LED",
            "After=local-fs.target",
            "",
            "[Service]",
            "Type=simple",
            f"ExecStart={boot_led_service_exec(status)}",
            "Restart=always",
            "RestartSec=2",
            "",
            "[Install]",
            "WantedBy=multi-user.target",
            "",
        ]
    )
    BOOT_LED_SERVICE_PATH.write_text(service_text, encoding="utf-8")
    run(["systemctl", "daemon-reload"])
    run(["systemctl", "enable", "--now", BOOT_LED_SERVICE_NAME], check=False)
    run(["systemctl", "restart", BOOT_LED_SERVICE_NAME])
    updated = boot_led_status()
    set_message(f"开机灯已更新为 {updated['duty']}% / {updated['hz']}Hz")
    return updated


def fan_control_service_exec():
    return "/usr/bin/python3 /opt/mido-wifi-portal/mido_fan_control.py --config /etc/mido-wifi-portal/fan-control.json"


def temperature_peak():
    sensors = temperature_sensors()
    if not sensors:
        return 0.0
    return round(max(float(item.get("celsius", 0.0)) for item in sensors), 1)


def load_fan_control_config():
    config = dict(FAN_CONTROL_DEFAULT)
    if FAN_CONTROL_CONFIG_PATH.exists():
        with FAN_CONTROL_CONFIG_PATH.open("r", encoding="utf-8") as fp:
            payload = json.load(fp)
        for key in config:
            if key in payload:
                config[key] = payload[key]
    return config


def save_fan_control_config(config):
    FAN_CONTROL_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    FAN_CONTROL_CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


def fan_control_status():
    config = load_fan_control_config()
    enabled, _ = systemctl_state(FAN_CONTROL_SERVICE_NAME, "is-enabled")
    active, _ = systemctl_state(FAN_CONTROL_SERVICE_NAME, "is-active")
    config["service"] = FAN_CONTROL_SERVICE_NAME
    config["service_exists"] = FAN_CONTROL_SERVICE_PATH.exists()
    config["enabled"] = bool(config.get("enabled")) and enabled
    config["active"] = active
    config["current_temp"] = temperature_peak()
    return config


def fan_control_update(payload):
    config = load_fan_control_config()
    config["enabled"] = bool(payload.get("enabled"))
    config["start_temp"] = parse_gpio_temperature(payload.get("start_temp"), "起转温度")
    config["full_temp"] = parse_gpio_temperature(payload.get("full_temp"), "满速温度")
    if config["full_temp"] <= config["start_temp"]:
        raise ValueError("满速温度必须高于起转温度")
    config["min_duty"] = parse_gpio_duty(payload.get("min_duty"))
    config["max_duty"] = parse_gpio_duty(payload.get("max_duty"))
    if config["max_duty"] < config["min_duty"]:
        raise ValueError("最大占空比不能低于最小占空比")
    config["hz"] = parse_gpio_frequency(payload.get("hz"))
    config["poll_interval"] = parse_gpio_interval(payload.get("poll_interval"))

    save_fan_control_config(config)

    service_text = "\n".join(
        [
            "[Unit]",
            "Description=Mido Temperature Controlled Fan",
            "After=local-fs.target",
            "",
            "[Service]",
            "Type=simple",
            f"ExecStart={fan_control_service_exec()}",
            "Restart=always",
            "RestartSec=2",
            "",
            "[Install]",
            "WantedBy=multi-user.target",
            "",
        ]
    )
    FAN_CONTROL_SERVICE_PATH.write_text(service_text, encoding="utf-8")
    run(["systemctl", "daemon-reload"])
    if config["enabled"]:
        run(["systemctl", "enable", "--now", FAN_CONTROL_SERVICE_NAME], check=False)
        run(["systemctl", "restart", FAN_CONTROL_SERVICE_NAME])
        set_message("温控风扇已启用")
    else:
        run(["systemctl", "disable", "--now", FAN_CONTROL_SERVICE_NAME], check=False)
        set_message("温控风扇已关闭")
    return fan_control_status()


def esp_gpio_link_service_exec():
    return "/usr/bin/python3 /opt/mido-wifi-portal/mido_esp_gpio_link.py --config /etc/mido-wifi-portal/esp-gpio-link.json"


def load_esp_gpio_link_config():
    config = dict(ESP_GPIO_LINK_DEFAULT)
    if ESP_GPIO_LINK_CONFIG_PATH.exists():
        with ESP_GPIO_LINK_CONFIG_PATH.open("r", encoding="utf-8") as fp:
            payload = json.load(fp)
        for key in config:
            if key in payload:
                config[key] = payload[key]
    return config


def save_esp_gpio_link_config(config):
    ESP_GPIO_LINK_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    ESP_GPIO_LINK_CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


def esp_gpio_link_runtime():
    if not ESP_GPIO_LINK_STATE_PATH.exists():
        return {}
    try:
        return json.loads(ESP_GPIO_LINK_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def esp_gpio_link_probe(url):
    target = str(url or "").strip()
    if not target:
        return {"ok": False, "message": "未配置 ESP 状态地址"}
    try:
        request = Request(target, headers={"User-Agent": "mido-wifi-portal"})
        with urlopen(request, timeout=1.5) as response:
            body = response.read().decode("utf-8", errors="replace")
        payload = json.loads(body) if body else {}
        if isinstance(payload, dict):
            payload.setdefault("ok", True)
            return payload
        return {"ok": False, "message": "ESP 返回格式异常"}
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        return {"ok": False, "message": str(exc)}


def esp_gpio_link_status():
    config = load_esp_gpio_link_config()
    service_enabled, _ = systemctl_state(ESP_GPIO_LINK_SERVICE_NAME, "is-enabled")
    service_active, _ = systemctl_state(ESP_GPIO_LINK_SERVICE_NAME, "is-active")
    runtime = esp_gpio_link_runtime()
    config["service"] = ESP_GPIO_LINK_SERVICE_NAME
    config["service_exists"] = ESP_GPIO_LINK_SERVICE_PATH.exists()
    config["service_enabled"] = service_enabled
    config["service_active"] = service_active
    config["runtime"] = runtime
    config["current_temp"] = runtime.get("temp_c", temperature_peak())
    config["esp_runtime"] = esp_gpio_link_probe(config.get("esp_api_url", ""))
    now = time.time()
    manual_until = float(config.get("manual_until", 0.0) or 0.0)
    config["manual_active"] = manual_until > now and (
        bool(config.get("manual_led_enabled")) or bool(config.get("manual_fan_enabled"))
    )
    config["manual_remaining_seconds"] = round(max(0.0, manual_until - now), 1) if config["manual_active"] else 0.0
    config["protocol_expected"] = {
        "magic": ESP_LINK_PROTOCOL_MAGIC,
        "magic_hex": f"0x{ESP_LINK_PROTOCOL_MAGIC:X}",
        "version": ESP_LINK_PROTOCOL_VERSION,
    }
    protocol = config["esp_runtime"].get("protocol")
    control = config["esp_runtime"].get("control")
    if isinstance(control, dict):
        control["fan_signal_duty"] = clamp_percent(control.get("fan_duty"))
        control["led_signal_duty"] = clamp_percent(control.get("led_duty"))
        protocol_synced = bool(protocol.get("synced")) if isinstance(protocol, dict) else False
        control_frames = int(control.get("frames", 0) or 0)
        if protocol_synced or control_frames > 0:
            control["fan_logical_duty"] = logical_duty_from_signal(
                control.get("fan_duty"),
                bool(config.get("fan_active_low")),
            )
            control["led_logical_duty"] = logical_duty_from_signal(
                control.get("led_duty"),
                bool(config.get("led_active_low")),
            )
        else:
            control["fan_logical_duty"] = None
            control["led_logical_duty"] = None
    if isinstance(protocol, dict):
        config["esp_runtime"]["protocol_ok"] = (
            int(protocol.get("magic", -1)) == ESP_LINK_PROTOCOL_MAGIC
            and int(protocol.get("version", -1)) == ESP_LINK_PROTOCOL_VERSION
            and bool(protocol.get("synced"))
        )
    return config


def esp_gpio_link_update(payload):
    config = load_esp_gpio_link_config()
    config["enabled"] = bool(payload.get("enabled"))
    config["start_temp"] = parse_gpio_temperature(payload.get("start_temp"), "起转温度")
    config["full_temp"] = parse_gpio_temperature(payload.get("full_temp"), "满速温度")
    if config["full_temp"] <= config["start_temp"]:
        raise ValueError("满速温度必须高于起转温度")
    config["min_duty"] = parse_gpio_duty(payload.get("min_duty"))
    config["max_duty"] = parse_gpio_duty(payload.get("max_duty"))
    if config["max_duty"] < config["min_duty"]:
        raise ValueError("最大占空比不能低于最小占空比")
    config["fan_active_low"] = bool(payload.get("fan_active_low"))
    config["led_duty"] = parse_gpio_duty(payload.get("led_duty"))
    config["led_active_low"] = bool(payload.get("led_active_low"))
    config["led_sleep_enabled"] = bool(payload.get("led_sleep_enabled"))
    config["led_sleep_after_minutes"] = parse_number_range(payload.get("led_sleep_after_minutes"), "休眠时长", 0, 1440)
    config["blink_on_disconnect"] = bool(payload.get("blink_on_disconnect"))
    config["blink_interval"] = parse_number_range(payload.get("blink_interval"), "断网闪烁周期", 0.2, 30)
    config["blink_duty"] = parse_gpio_duty(payload.get("blink_duty"))
    config["frame_interval"] = parse_gpio_interval(payload.get("frame_interval"))
    config["manual_timeout_seconds"] = parse_number_range(
        payload.get("manual_timeout_seconds", config.get("manual_timeout_seconds", 120)),
        "测试保持时长",
        1,
        3600,
    )
    # 保存基础参数时直接退出测试接管，避免上一次开关测试继续覆盖默认输出。
    config["manual_until"] = 0.0
    config["manual_led_enabled"] = False
    config["manual_led_duty"] = 0.0
    config["manual_fan_enabled"] = False
    config["manual_fan_duty"] = 0.0

    save_esp_gpio_link_config(config)

    service_text = "\n".join(
        [
            "[Unit]",
            "Description=Mido GPIO64 to ESP8266 Single-Wire Link",
            "After=network-online.target",
            "Wants=network-online.target",
            "",
            "[Service]",
            "Type=simple",
            f"ExecStart={esp_gpio_link_service_exec()}",
            "Restart=always",
            "RestartSec=2",
            "",
            "[Install]",
            "WantedBy=multi-user.target",
            "",
        ]
    )
    ESP_GPIO_LINK_SERVICE_PATH.write_text(service_text, encoding="utf-8")
    run(["systemctl", "disable", "--now", BOOT_LED_SERVICE_NAME], check=False)
    try:
        gpio_release(str(config["chip"]).rsplit("/", 1)[-1], str(config["line"]))
    except Exception:
        pass
    run(["systemctl", "daemon-reload"])
    run(["systemctl", "enable", "--now", ESP_GPIO_LINK_SERVICE_NAME], check=False)
    run(["systemctl", "restart", ESP_GPIO_LINK_SERVICE_NAME])
    set_message("ESP 单线控制参数已更新，并已退出测试模式")
    return esp_gpio_link_status()


def esp_gpio_link_test(payload):
    config = load_esp_gpio_link_config()
    runtime = esp_gpio_link_runtime()
    action = str(payload.get("action", "")).strip()
    timeout_seconds = parse_number_range(
        payload.get("timeout_seconds", config.get("manual_timeout_seconds", 120)),
        "测试保持时长",
        1,
        3600,
    )
    config["manual_timeout_seconds"] = timeout_seconds
    current_fan_duty = parse_gpio_duty(runtime.get("fan_duty", 0))
    current_led_duty = parse_gpio_duty(runtime.get("led_duty", 0))

    if action == "restore":
        config["manual_until"] = 0.0
        config["manual_led_enabled"] = False
        config["manual_led_duty"] = 0.0
        config["manual_fan_enabled"] = False
        config["manual_fan_duty"] = 0.0
        set_message("ESP 测试输出已恢复自动模式")
    elif action == "led_on":
        config["manual_until"] = time.time() + timeout_seconds
        config["manual_led_enabled"] = True
        config["manual_led_duty"] = parse_gpio_duty(payload.get("led_duty", 100))
        config["manual_fan_enabled"] = True
        config["manual_fan_duty"] = current_fan_duty
        set_message(f"指示灯测试已开启 {config['manual_led_duty']}%，保持 {int(timeout_seconds)} 秒")
    elif action == "led_off":
        config["manual_until"] = time.time() + timeout_seconds
        config["manual_led_enabled"] = True
        config["manual_led_duty"] = 0.0
        config["manual_fan_enabled"] = True
        config["manual_fan_duty"] = current_fan_duty
        set_message(f"指示灯测试已关闭，保持 {int(timeout_seconds)} 秒")
    elif action == "fan_on":
        config["manual_until"] = time.time() + timeout_seconds
        config["manual_fan_enabled"] = True
        config["manual_fan_duty"] = parse_gpio_duty(payload.get("fan_duty", 100))
        config["manual_led_enabled"] = True
        config["manual_led_duty"] = current_led_duty
        set_message(f"风扇测试已开启 {config['manual_fan_duty']}%，保持 {int(timeout_seconds)} 秒")
    elif action == "fan_off":
        config["manual_until"] = time.time() + timeout_seconds
        config["manual_fan_enabled"] = True
        config["manual_fan_duty"] = 0.0
        config["manual_led_enabled"] = True
        config["manual_led_duty"] = current_led_duty
        set_message(f"风扇测试已关闭，保持 {int(timeout_seconds)} 秒")
    else:
        raise ValueError("未知测试动作")

    save_esp_gpio_link_config(config)
    return esp_gpio_link_status()


def collect_status():
    with STATE_LOCK:
        message = STATE["last_message"]
        hotspot_active = STATE["hotspot_active"]
        last_scan_at = STATE["last_scan_at"]
    request_status_refresh(force=False)
    with STATUS_CACHE_LOCK:
        slow_data = dict(STATUS_CACHE["slow_data"])
        slow_updated_at = STATUS_CACHE["updated_at"]
        slow_refreshing = STATUS_CACHE["refreshing"]

    counters = interface_counters(CONFIG["wifi_interface"])
    fast_ip4 = ip4_address_fast()
    fast_dns = dns_servers_fast()
    current_ip4 = fast_ip4 or slow_data.get("ip4", "")
    current_dns = fast_dns or slow_data.get("dns_servers", [])
    saved_wifi_count = slow_data.get("saved_wifi_count")
    if saved_wifi_count is None:
        saved_wifi_count = saved_wifi_count_fast()

    return {
        "hostname": socket.gethostname(),
        "wifi_interface": CONFIG["wifi_interface"],
        "ip4": current_ip4,
        "dns_servers": current_dns,
        "connectivity": slow_data.get("connectivity", "unknown"),
        "uplink": slow_data.get("uplink", bool(current_ip4)),
        "hotspot_active": hotspot_active or slow_data.get("hotspot_active", False),
        "hotspot_ssid": CONFIG["hotspot_ssid"],
        "saved_wifi_count": saved_wifi_count,
        "system": system_summary(),
        "network_stats": {**counters, "sample_at": time.time()},
        "device_status": slow_data.get("device_status", []),
        "active_connections": slow_data.get("active_connections", []),
        "services": slow_data.get("services", {}),
        "last_message": message,
        "last_scan_at": last_scan_at,
        "status_updated_at": slow_updated_at,
        "status_refreshing": slow_refreshing,
    }


def page_html():
    return """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Mido WiFi Portal</title>
  <style>
    :root {
      --bg: #eef3f7;
      --card: #ffffff;
      --line: #d6e0e7;
      --text: #1d2a33;
      --muted: #667887;
      --accent: #1f8f63;
      --accent-2: #0d6e9e;
      --warn: #d27f00;
      --shadow: 0 12px 28px rgba(28, 51, 66, 0.10);
      --radius: 18px;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
      color: var(--text);
      background:
        radial-gradient(circle at top left, rgba(31,143,99,0.10), transparent 32%),
        radial-gradient(circle at bottom right, rgba(13,110,158,0.10), transparent 30%),
        var(--bg);
    }
    .wrap {
      max-width: 1120px;
      margin: 0 auto;
      padding: 28px 20px 40px;
    }
    .hero {
      display: grid;
      grid-template-columns: 1.2fr .8fr;
      gap: 18px;
      margin-bottom: 18px;
    }
    .card {
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
      padding: 20px;
    }
    h1 {
      margin: 0 0 10px;
      font-size: 30px;
      letter-spacing: 0.02em;
    }
    h2 {
      margin: 0 0 14px;
      font-size: 18px;
    }
    .muted { color: var(--muted); }
    .status-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
    }
    .pill {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 8px 12px;
      border-radius: 999px;
      background: #f4f8fb;
      border: 1px solid var(--line);
      font-size: 13px;
    }
    .ok { color: var(--accent); }
    .warn { color: var(--warn); }
    .neutral { color: var(--accent-2); }
    .actions, .toolbar {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
    }
    button {
      border: 0;
      border-radius: 12px;
      padding: 10px 14px;
      cursor: pointer;
      color: #fff;
      background: linear-gradient(135deg, var(--accent), #27a273);
      font-weight: 600;
    }
    button.secondary {
      background: linear-gradient(135deg, var(--accent-2), #2892c8);
    }
    .layout {
      display: grid;
      grid-template-columns: 1.1fr .9fr;
      gap: 18px;
    }
    .mini-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 18px;
      margin-top: 18px;
    }
    .network {
      display: grid;
      grid-template-columns: 1fr auto auto;
      gap: 10px;
      align-items: center;
      padding: 12px 0;
      border-bottom: 1px solid #edf2f5;
    }
    .network:last-child { border-bottom: 0; }
    .network-title { font-weight: 600; }
    .signal {
      min-width: 56px;
      text-align: right;
      color: var(--muted);
    }
    .form-grid {
      display: grid;
      gap: 12px;
    }
    input {
      width: 100%;
      padding: 11px 12px;
      border-radius: 12px;
      border: 1px solid var(--line);
      background: #fbfdfe;
      color: var(--text);
    }
    textarea, select {
      width: 100%;
      padding: 11px 12px;
      border-radius: 12px;
      border: 1px solid var(--line);
      background: #fbfdfe;
      color: var(--text);
      font: inherit;
    }
    textarea {
      min-height: 160px;
      resize: vertical;
      font-family: "Consolas", "Courier New", monospace;
    }
    .log {
      min-height: 48px;
      padding: 12px;
      border-radius: 14px;
      background: #f7fafc;
      border: 1px dashed var(--line);
      color: var(--muted);
    }
    pre.log {
      white-space: pre-wrap;
      word-break: break-word;
      font-family: "Consolas", "Courier New", monospace;
      max-height: 360px;
      overflow: auto;
    }
    .chip {
      padding: 12px 0;
      border-bottom: 1px solid #edf2f5;
    }
    .chip:last-child { border-bottom: 0; }
    @media (max-width: 860px) {
      .hero, .layout, .status-grid, .mini-grid { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="hero">
      <section class="card">
        <h1>Mido WiFi Portal</h1>
        <p class="muted">无头主板配网面板。当前没有外网时会自动拉起热点，联网恢复后自动退出热点。</p>
        <div class="actions">
          <button onclick="refreshAll()">刷新状态</button>
          <button class="secondary" onclick="toggleHotspot('start')">手动开热点</button>
          <button class="secondary" onclick="toggleHotspot('stop')">手动关热点</button>
          <button class="secondary" onclick="document.getElementById('console-command').focus()">系统控制台</button>
          <button class="secondary" onclick="document.getElementById('gpio-chip').focus()">GPIO 调试</button>
        </div>
      </section>
      <section class="card">
        <h2>运行状态</h2>
        <div id="status-grid" class="status-grid"></div>
      </section>
    </div>

    <div class="layout">
      <section class="card">
        <div class="toolbar">
          <h2 style="flex:1">附近 Wi-Fi</h2>
          <button class="secondary" onclick="loadNetworks()">刷新列表</button>
        </div>
        <div id="network-note" class="muted" style="margin-bottom: 10px;"></div>
        <div id="network-list"></div>
      </section>

      <section class="card">
        <h2>连接网络</h2>
        <div class="form-grid">
          <input id="ssid" placeholder="SSID">
          <input id="password" type="password" placeholder="密码">
          <label><input id="hidden" type="checkbox"> 隐藏网络</label>
          <button onclick="connectWifi()">保存并连接</button>
        </div>
        <h2 style="margin-top: 22px;">消息</h2>
        <div id="log" class="log">等待状态...</div>
      </section>
    </div>

    <div class="mini-grid">
      <section class="card">
        <div class="toolbar">
          <h2 style="flex:1">系统控制台</h2>
          <button class="secondary" onclick="setConsoleCommand('systemctl status mido-wifi-portal.service --no-pager -n 40')">服务状态</button>
          <button class="secondary" onclick="setConsoleCommand('nmcli device status')">网络状态</button>
        </div>
        <div class="form-grid">
          <textarea id="console-command" placeholder="输入 shell 命令，例如 ip a 或 journalctl -u mido-wifi-portal.service -n 80 --no-pager"></textarea>
          <div class="actions">
            <button onclick="runConsole()">执行命令</button>
            <button class="secondary" onclick="clearConsole()">清空输出</button>
          </div>
        </div>
        <pre id="console-output" class="log">等待执行命令...</pre>
      </section>

      <section class="card">
        <div class="toolbar">
          <h2 style="flex:1">GPIO 调试</h2>
          <button class="secondary" onclick="refreshGPIO()">刷新 GPIO</button>
        </div>
        <div class="form-grid">
          <select id="gpio-chip"></select>
          <input id="gpio-line" type="number" min="0" placeholder="line offset，例如 42">
          <div class="actions">
            <button onclick="writeGPIO(1)">输出高电平</button>
            <button class="secondary" onclick="writeGPIO(0)">输出低电平</button>
            <button class="secondary" onclick="readGPIO()">读取当前值</button>
            <button class="secondary" onclick="releaseGPIO()">释放</button>
          </div>
        </div>
        <div id="gpio-log" class="log">等待 GPIO 操作...</div>
        <div id="gpio-list" style="margin-top: 14px;"></div>
      </section>
    </div>
  </div>

  <script>
    async function jget(url, options) {
      const res = await fetch(url, options || {});
      if (!res.ok) throw new Error(await res.text());
      return await res.json();
    }

    function statusPill(label, value, ok=true) {
      let cls = 'ok';
      if (ok === false) cls = 'warn';
      if (ok === null) cls = 'neutral';
      return `<div class="pill ${cls}"><strong>${label}</strong><span>${value}</span></div>`;
    }

    function renderStatus(data) {
      const connectivityOk = ['full', 'limited', 'portal'].includes((data.connectivity || '').toLowerCase());
      const html = [
        statusPill('主机名', data.hostname),
        statusPill('接口', data.wifi_interface),
        statusPill('IPv4', data.ip4 || '无'),
        statusPill('外网', data.uplink ? '可用' : '不可用', data.uplink),
        statusPill('NM 状态', data.connectivity || 'unknown', connectivityOk ? true : null),
        statusPill('热点', data.hotspot_active ? ('开启: ' + data.hotspot_ssid) : '关闭', data.hotspot_active),
        statusPill('连接数', String((data.active_connections || []).length))
      ].join('');
      document.getElementById('status-grid').innerHTML = html;
      document.getElementById('log').textContent = data.last_message || '无新消息';
    }

    function renderNetworks(data) {
      document.getElementById('network-note').textContent = data.message || '';
      const fixed = (data.networks || []).map((item) => {
        const ssid = (item.ssid || '').replace(/'/g, "\\'");
        return `
        <div class="network">
          <div>
            <div class="network-title">${item.ssid || '(隐藏网络)'}</div>
            <div class="muted">${item.security || '开放网络'}${item.in_use ? ' · 当前连接' : ''}</div>
          </div>
          <div class="signal">${item.signal || '?'}%</div>
          <button onclick="fillForm('${ssid}')">填入</button>
        </div>`;
      }).join('');

      document.getElementById('network-list').innerHTML = fixed || '<div class="muted">暂无可用扫描结果，可手动输入 SSID。</div>';
    }

    function fillForm(ssid) {
      document.getElementById('ssid').value = ssid;
    }

    function setConsoleCommand(command) {
      document.getElementById('console-command').value = command;
    }

    function renderGPIO(data) {
      const select = document.getElementById('gpio-chip');
      if (!data.available) {
        select.innerHTML = '<option value="">未安装 gpiod</option>';
        document.getElementById('gpio-list').innerHTML = '<div class="log">系统未安装 gpiod，GPIO 页面暂不可用。</div>';
        return;
      }

      const chips = data.chips || [];
      const current = select.value;
      select.innerHTML = chips.map((item) => `<option value="${item.chip}">${item.chip} · ${item.label} · ${item.lines} lines</option>`).join('');
      if (current && chips.some((item) => item.chip === current)) {
        select.value = current;
      }

      const holds = new Map((data.holds || []).map((item) => [`${item.chip}:${item.line}`, item.value]));
      const html = chips.map((item) => {
        const active = [];
        holds.forEach((value, key) => {
          if (key.startsWith(`${item.chip}:`)) {
            active.push(`${key.split(':')[1]}=${value}`);
          }
        });
        return `
        <div class="chip">
          <div class="network-title">${item.chip}</div>
          <div class="muted">${item.label} · ${item.lines} lines${active.length ? ' · 保持中: ' + active.join(', ') : ''}</div>
        </div>`;
      }).join('');
      document.getElementById('gpio-list').innerHTML = html || '<div class="muted">未检测到 GPIO 芯片。</div>';
    }

    async function refreshAll() {
      const [status, networks, gpio] = await Promise.all([
        jget('/api/status'),
        jget('/api/networks'),
        jget('/api/gpio/chips')
      ]);
      renderStatus(status);
      renderNetworks(networks);
      renderGPIO(gpio);
    }

    async function loadNetworks() {
      renderNetworks(await jget('/api/networks'));
    }

    async function refreshGPIO() {
      renderGPIO(await jget('/api/gpio/chips'));
    }

    async function toggleHotspot(action) {
      const data = await jget('/api/hotspot', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({action})
      });
      renderStatus(await jget('/api/status'));
      document.getElementById('log').textContent = data.message || '操作完成';
    }

    async function connectWifi() {
      const payload = {
        ssid: document.getElementById('ssid').value.trim(),
        password: document.getElementById('password').value,
        hidden: document.getElementById('hidden').checked
      };
      const data = await jget('/api/connect', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload)
      });
      document.getElementById('log').textContent = data.message || '已提交连接';
      setTimeout(refreshAll, 1200);
    }

    async function runConsole() {
      const data = await jget('/api/console', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({command: document.getElementById('console-command').value})
      });
      document.getElementById('console-output').textContent = `[exit=${data.exit_code}${data.timeout ? ', timeout' : ''}]\\n${data.output || '(no output)'}`;
    }

    function clearConsole() {
      document.getElementById('console-output').textContent = '等待执行命令...';
    }

    function gpioPayload() {
      return {
        chip: document.getElementById('gpio-chip').value,
        line: document.getElementById('gpio-line').value
      };
    }

    async function readGPIO() {
      const data = await jget('/api/gpio/read', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(gpioPayload())
      });
      document.getElementById('gpio-log').textContent = `GPIO ${data.chip}:${data.line} 当前值 ${data.value}${data.held ? '，由面板保持中' : ''}`;
    }

    async function writeGPIO(value) {
      const payload = gpioPayload();
      payload.value = value;
      const data = await jget('/api/gpio/write', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload)
      });
      document.getElementById('gpio-log').textContent = `GPIO ${data.chip}:${data.line} 已保持为 ${data.value}`;
      await refreshGPIO();
    }

    async function releaseGPIO() {
      const data = await jget('/api/gpio/release', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(gpioPayload())
      });
      document.getElementById('gpio-log').textContent = data.released
        ? `GPIO ${data.chip}:${data.line} 已释放`
        : `GPIO ${data.chip}:${data.line} 当前没有被面板保持`;
      await refreshGPIO();
    }

    refreshAll().catch((err) => {
      document.getElementById('log').textContent = String(err);
      document.getElementById('console-output').textContent = String(err);
      document.getElementById('gpio-log').textContent = String(err);
    });
  </script>
</body>
</html>"""


class PortalHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/":
                body = Path(__file__).with_name("portal.html").read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
                self.send_header("Pragma", "no-cache")
                self.send_header("Expires", "0")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if parsed.path in {
                "/generate_204",
                "/hotspot-detect.html",
                "/connecttest.txt",
                "/ncsi.txt",
                "/success.txt",
                "/redirect",
            }:
                self.send_response(HTTPStatus.FOUND)
                self.send_header("Location", "/")
                self.end_headers()
                return

            if parsed.path == "/api/status":
                return json_response(self, collect_status())

            if parsed.path == "/api/networks":
                return json_response(self, scan_networks())

            if parsed.path == "/api/wifi/saved":
                return json_response(self, {"profiles": saved_wifi_connections()})

            if parsed.path == "/api/docker/containers":
                return json_response(self, list_docker_containers())

            if parsed.path == "/api/docker/logs":
                params = parse_qs(parsed.query)
                name = (params.get("name") or [""])[0].strip()
                tail_text = (params.get("tail") or ["120"])[0].strip()
                try:
                    tail = int(tail_text or "120")
                except ValueError:
                    tail = 120
                return json_response(self, docker_container_logs(name, tail=tail))

            if parsed.path == "/api/terminal/output":
                params = parse_qs(parsed.query)
                session_id = (params.get("session") or [""])[0].strip()
                cursor_text = (params.get("cursor") or ["0"])[0].strip()
                try:
                    cursor = int(cursor_text or "0")
                except ValueError:
                    cursor = 0
                return json_response(self, terminal_output(session_id, cursor))

            if parsed.path == "/api/openclash/status":
                return json_response(self, openclash_status())

            if parsed.path == "/api/openclash/proxies":
                return json_response(self, openclash_proxy_groups())

            if parsed.path == "/api/openclash/subscriptions":
                return json_response(self, list_openclash_subscriptions())

            if parsed.path == "/api/openclash/config":
                return json_response(self, read_openclash_config())

            if parsed.path == "/api/gpio/chips":
                return json_response(self, gpio_chips())

            if parsed.path == "/api/gpio/boot-led":
                return json_response(self, boot_led_status())

            if parsed.path == "/api/gpio/fan-control":
                return json_response(self, fan_control_status())

            if parsed.path == "/api/esp-link":
                return json_response(self, esp_gpio_link_status())
        except Exception as exc:
            if parsed.path.startswith("/api/"):
                return json_response(self, {"ok": False, "message": str(exc)}, HTTPStatus.BAD_REQUEST)
            raise

        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", "/")
        self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            payload = {}

        try:
            if parsed.path == "/api/connect":
                message = connect_wifi(payload.get("ssid", "").strip(), payload.get("password", ""), bool(payload.get("hidden")))
                request_status_refresh(force=True)
                return json_response(self, {"ok": True, "message": message})

            if parsed.path == "/api/hotspot":
                action = payload.get("action")
                if action == "start":
                    start_hotspot()
                    request_status_refresh(force=True)
                    return json_response(self, {"ok": True, "message": "热点已启动"})
                if action == "stop":
                    stop_hotspot()
                    request_status_refresh(force=True)
                    return json_response(self, {"ok": True, "message": "热点已关闭"})
                return json_response(self, {"ok": False, "message": "未知动作"}, HTTPStatus.BAD_REQUEST)

            if parsed.path == "/api/wifi/activate":
                message = activate_wifi_connection(payload.get("uuid", "").strip())
                request_status_refresh(force=True)
                return json_response(self, {"ok": True, "message": message})

            if parsed.path == "/api/wifi/delete":
                message = delete_wifi_connection(payload.get("uuid", "").strip())
                request_status_refresh(force=True)
                return json_response(self, {"ok": True, "message": message})

            if parsed.path == "/api/docker/action":
                message = docker_container_action(payload.get("name", "").strip(), payload.get("action", "").strip())
                request_status_refresh(force=True)
                return json_response(self, {"ok": True, "message": message})

            if parsed.path == "/api/openclash/action":
                message = openclash_action(payload.get("action", "").strip())
                request_status_refresh(force=True)
                return json_response(self, {"ok": True, "message": message})

            if parsed.path == "/api/openclash/proxy/select":
                message = openclash_switch_proxy(payload.get("group", "").strip(), payload.get("name", "").strip())
                return json_response(self, {"ok": True, "message": message})

            if parsed.path == "/api/openclash/subscription":
                result = upsert_openclash_subscription(
                    payload.get("name", "").strip(),
                    payload.get("url", "").strip(),
                    bool(payload.get("activate")),
                )
                return json_response(self, {"ok": True, "message": f"订阅 {result['name']} 已保存", **result})

            if parsed.path == "/api/openclash/subscription/refresh":
                result = refresh_openclash_subscription(payload.get("name", "").strip(), bool(payload.get("activate")))
                return json_response(self, {"ok": True, "message": f"订阅 {result['name']} 已刷新", **result})

            if parsed.path == "/api/openclash/subscription/activate":
                result = activate_openclash_subscription(payload.get("name", "").strip())
                return json_response(self, {"ok": True, "message": f"订阅 {result['name']} 已启用", **result})

            if parsed.path == "/api/openclash/subscription/delete":
                result = delete_openclash_subscription(payload.get("name", "").strip())
                return json_response(self, {"ok": True, "message": f"订阅 {result['name']} 已删除", **result})

            if parsed.path == "/api/openclash/config":
                path = save_openclash_config(payload.get("content", ""), bool(payload.get("restart")))
                return json_response(self, {"ok": True, "message": f"已保存配置：{path}", "path": path})

            if parsed.path == "/api/service/action":
                message = service_action(payload.get("service", "").strip(), payload.get("action", "").strip())
                request_status_refresh(force=True)
                return json_response(self, {"ok": True, "message": message})

            if parsed.path == "/api/console":
                result = run_console(payload.get("command", ""))
                return json_response(self, {"ok": True, **result})

            if parsed.path == "/api/terminal/session":
                result = create_terminal_session()
                return json_response(self, {"ok": True, **result})

            if parsed.path == "/api/terminal/input":
                result = terminal_input(payload.get("session_id", "").strip(), payload.get("data", ""))
                return json_response(self, {"ok": True, **result})

            if parsed.path == "/api/terminal/close":
                result = close_terminal_session(payload.get("session_id", "").strip())
                return json_response(self, {"ok": True, **result})

            if parsed.path == "/api/gpio/read":
                result = gpio_read(payload.get("chip", "").strip(), payload.get("line", "0"))
                return json_response(self, {"ok": True, **result})

            if parsed.path == "/api/gpio/write":
                result = gpio_write(payload.get("chip", "").strip(), payload.get("line", "0"), payload.get("value", 0))
                return json_response(self, {"ok": True, **result})

            if parsed.path == "/api/gpio/pwm":
                result = gpio_pwm(
                    payload.get("chip", "").strip(),
                    payload.get("line", "0"),
                    payload.get("duty", ""),
                    payload.get("hz", ""),
                )
                return json_response(self, {"ok": True, **result})

            if parsed.path == "/api/gpio/release":
                result = gpio_release(payload.get("chip", "").strip(), payload.get("line", "0"))
                return json_response(self, {"ok": True, **result})

            if parsed.path == "/api/gpio/boot-led":
                result = boot_led_update(payload.get("duty", ""), payload.get("hz", ""))
                return json_response(self, {"ok": True, **result})

            if parsed.path == "/api/gpio/fan-control":
                result = fan_control_update(payload)
                return json_response(self, {"ok": True, **result})

            if parsed.path == "/api/esp-link":
                result = esp_gpio_link_update(payload)
                return json_response(self, {"ok": True, **result})

            if parsed.path == "/api/esp-link/test":
                result = esp_gpio_link_test(payload)
                return json_response(self, {"ok": True, **result})
        except Exception as exc:
            return json_response(self, {"ok": False, "message": str(exc)}, HTTPStatus.BAD_REQUEST)

        self.send_error(HTTPStatus.NOT_FOUND)

    def log_message(self, fmt, *args):
        print(f"[http] {self.address_string()} - {fmt % args}")


def monitor_loop():
    offline_count = 0
    online_count = 0

    while True:
        try:
            online = has_uplink()
            hotspot_active = hotspot_is_active()
            with STATE_LOCK:
                STATE["hotspot_active"] = hotspot_active
                suppress_until = STATE["suppress_hotspot_until"]

            if online:
                offline_count = 0
                online_count += 1
                if hotspot_active and online_count >= int(CONFIG["online_threshold"]):
                    stop_hotspot()
            else:
                online_count = 0
                offline_count += 1
                if time.time() >= suppress_until and not hotspot_active and offline_count >= int(CONFIG["offline_threshold"]):
                    start_hotspot()
        except Exception as exc:
            set_message(f"监控异常: {exc}")

        time.sleep(int(CONFIG["check_interval"]))


def main():
    threading.Thread(target=monitor_loop, daemon=True).start()
    request_status_refresh(force=True)
    server = ThreadingHTTPServer((CONFIG["bind_host"], int(CONFIG["bind_port"])), PortalHandler)
    print(f"mido wifi portal listening on {CONFIG['bind_host']}:{CONFIG['bind_port']}")
    server.serve_forever()


if __name__ == "__main__":
    main()
