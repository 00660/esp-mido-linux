#!/usr/bin/env python3
import argparse
import json
import signal
import subprocess
import sys
import time
from pathlib import Path


STOP = False
STATE_PATH = Path("/run/mido-esp-gpio-link-state.json")
PROTOCOL_MAGIC = 0x0A
PROTOCOL_VERSION = 1
DEFAULT_CONFIG = {
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


def parse_args():
    parser = argparse.ArgumentParser(description="Mido GPIO64 -> ESP8266 单线控制服务")
    parser.add_argument("--config", default="/etc/mido-wifi-portal/esp-gpio-link.json", help="配置文件路径")
    parser.add_argument("--consumer", default="mido-esp-gpio-link", help="GPIO consumer 名称")
    return parser.parse_args()


def stop_handler(signum, frame):
    del signum, frame
    global STOP
    STOP = True


class GpioLine:
    def __init__(self, chip_name, offset, consumer, idle_high):
        self.offset = int(offset)
        self.consumer = consumer
        self.idle_high = bool(idle_high)
        self.backend = None
        self.request = None
        self.line = None
        self.chip = None

        try:
            import gpiod  # type: ignore
        except ImportError as exc:
            raise RuntimeError("未安装 python3-libgpiod，无法驱动 GPIO") from exc

        self.gpiod = gpiod
        self._open_v2(chip_name)

    def _open_v2(self, chip_name):
        try:
            line_module = self.gpiod.line
            settings = self.gpiod.LineSettings(
                direction=line_module.Direction.OUTPUT,
                output_value=line_module.Value.ACTIVE if self.idle_high else line_module.Value.INACTIVE,
            )
            self.request = self.gpiod.request_lines(
                chip_name,
                consumer=self.consumer,
                config={self.offset: settings},
            )
            self.backend = "v2"
        except Exception:
            self._open_v1(chip_name)

    def _open_v1(self, chip_name):
        chip_target = chip_name
        if str(chip_name).startswith("/dev/"):
            chip_target = str(chip_name).rsplit("/", 1)[-1]
        self.chip = self.gpiod.Chip(chip_target)
        self.line = self.chip.get_line(self.offset)
        self.line.request(
            consumer=self.consumer,
            type=self.gpiod.LINE_REQ_DIR_OUT,
            default_vals=[1 if self.idle_high else 0],
        )
        self.backend = "v1"

    def set_value(self, high):
        value = 1 if high else 0
        if self.backend == "v2":
            line_module = self.gpiod.line
            self.request.set_value(
                self.offset,
                line_module.Value.ACTIVE if value else line_module.Value.INACTIVE,
            )
            return
        self.line.set_value(value)

    def close(self):
        try:
            self.set_value(self.idle_high)
        except Exception:
            pass

        if self.backend == "v2" and self.request is not None:
            try:
                self.request.release()
            except Exception:
                pass
        if self.backend == "v1" and self.line is not None:
            try:
                self.line.release()
            except Exception:
                pass


def load_config(path):
    config = dict(DEFAULT_CONFIG)
    cfg_path = Path(path)
    if cfg_path.exists():
        with cfg_path.open("r", encoding="utf-8") as fp:
            payload = json.load(fp)
        for key in config:
            if key in payload:
                config[key] = payload[key]
    return config


def run(cmd, check=False):
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def temperature_peak():
    peak = 0.0
    for path in sorted(Path("/sys/class/thermal").glob("thermal_zone*")):
        temp_path = path / "temp"
        if not temp_path.exists():
            continue
        try:
            raw = float(temp_path.read_text(encoding="utf-8").strip())
            celsius = raw / 1000.0 if raw > 1000 else raw
            if -50 <= celsius <= 200:
                peak = max(peak, celsius)
        except Exception:
            continue
    return peak


def clamp_percent(value):
    return max(0.0, min(100.0, float(value)))


def uptime_seconds():
    try:
        return float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0])
    except Exception:
        return 0.0


def connectivity_state():
    proc = run(["nmcli", "networking", "connectivity"], check=False)
    return (proc.stdout or "").strip().lower() or "unknown"


def uplink_available():
    connectivity = connectivity_state()
    if connectivity in {"full", "limited", "portal"}:
        return True, connectivity

    route = run(["ip", "route", "show", "default"], check=False)
    if route.stdout.strip():
        return True, connectivity or "route"
    return False, connectivity or "none"


def duty_from_temp(temp, config):
    if not config.get("enabled", True):
        return 0.0

    start_temp = float(config["start_temp"])
    full_temp = float(config["full_temp"])
    min_duty = clamp_percent(config["min_duty"])
    max_duty = clamp_percent(config["max_duty"])

    if full_temp <= start_temp:
        full_temp = start_temp + 1.0

    if temp < start_temp:
        return 0.0
    if temp >= full_temp:
        return max_duty

    ratio = (temp - start_temp) / (full_temp - start_temp)
    return min_duty + ratio * (max_duty - min_duty)


def duty_to_byte(duty):
    return int(round(clamp_percent(duty) * 255.0 / 100.0))


def byte_to_duty(value):
    return round(max(0, min(255, int(value))) * 100.0 / 255.0, 1)


def effective_led_duty(config, uptime_s, uplink, now_monotonic):
    if not config.get("enabled", True):
        return 0.0, False, False

    base_duty = clamp_percent(config.get("led_duty", 10.0))
    blink_active = bool(config.get("blink_on_disconnect")) and not uplink
    sleeping = False

    if blink_active:
        blink_interval = max(0.25, float(config.get("blink_interval", 0.8)))
        blink_duty = clamp_percent(config.get("blink_duty", max(base_duty, 25.0)))
        phase_on = int(now_monotonic / blink_interval) % 2 == 0
        return (blink_duty if phase_on else 0.0), False, True

    if bool(config.get("led_sleep_enabled")):
        sleep_after_minutes = max(0.0, float(config.get("led_sleep_after_minutes", 5.0)))
        if uptime_s >= sleep_after_minutes * 60.0:
            sleeping = True
            return 0.0, sleeping, False

    return base_duty, sleeping, False


def apply_trigger_mode(duty, active_low):
    duty = clamp_percent(duty)
    return round(100.0 - duty, 1) if active_low else duty


def write_state(payload):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = STATE_PATH.with_suffix(".tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(STATE_PATH)


def sleep_interruptible(seconds):
    deadline = time.monotonic() + max(0.0, float(seconds))
    while not STOP:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 0.01))


def send_low_pulse(gpio, low_seconds, gap_seconds, idle_high):
    gpio.set_value(not idle_high)
    sleep_interruptible(low_seconds)
    gpio.set_value(idle_high)
    sleep_interruptible(gap_seconds)


def build_legacy_4bit_frame_nibbles(fan_duty, led_duty):
    fan_nibble = int(round(clamp_percent(fan_duty) * 15.0 / 100.0)) & 0x0F
    led_nibble = int(round(clamp_percent(led_duty) * 15.0 / 100.0)) & 0x0F
    checksum = (fan_nibble ^ led_nibble) & 0x0F
    return [fan_nibble, led_nibble, checksum]


def send_legacy_4bit_frame(gpio, fan_duty, led_duty, config):
    idle_high = bool(config.get("idle_high", True))
    nibbles = build_legacy_4bit_frame_nibbles(fan_duty, led_duty)
    base_seconds = max(0.001, float(config["symbol_base_ms"]) / 1000.0)
    step_seconds = max(0.001, float(config["symbol_step_ms"]) / 1000.0)
    gap_seconds = max(0.001, float(config["symbol_gap_ms"]) / 1000.0)
    sync_seconds = max(0.010, float(config["sync_ms"]) / 1000.0)

    send_low_pulse(gpio, sync_seconds, gap_seconds, idle_high)
    for symbol in nibbles:
        send_low_pulse(gpio, base_seconds + (int(symbol) * step_seconds), gap_seconds, idle_high)


def build_gpio(config, consumer):
    return GpioLine(config["chip"], int(config["line"]), consumer, bool(config.get("idle_high", True)))


def manual_override_values(config, now_wall_time):
    manual_until = float(config.get("manual_until", 0.0) or 0.0)
    manual_active = manual_until > now_wall_time
    remaining = max(0.0, manual_until - now_wall_time) if manual_active else 0.0
    return {
        "active": manual_active,
        "remaining_seconds": round(remaining, 1),
        "led_enabled": manual_active and bool(config.get("manual_led_enabled")),
        "fan_enabled": manual_active and bool(config.get("manual_fan_enabled")),
        "led_duty": clamp_percent(config.get("manual_led_duty", 0.0)),
        "fan_duty": clamp_percent(config.get("manual_fan_duty", 0.0)),
    }


def main():
    args = parse_args()
    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)

    config = load_config(args.config)
    gpio = build_gpio(config, args.consumer)
    last_gpio_signature = (config["chip"], int(config["line"]), bool(config.get("idle_high", True)))
    last_log_at = 0.0
    last_uplink_refresh_at = 0.0
    cached_uplink = True
    cached_connectivity = "unknown"
    last_tx_type = "single_wire"
    legacy_compat = True

    try:
        while not STOP:
            loop_started = time.monotonic()
            config = load_config(args.config)
            gpio_signature = (config["chip"], int(config["line"]), bool(config.get("idle_high", True)))
            if gpio_signature != last_gpio_signature:
                gpio.close()
                gpio = build_gpio(config, args.consumer)
                last_gpio_signature = gpio_signature

            if loop_started - last_uplink_refresh_at >= 5.0:
                cached_uplink, cached_connectivity = uplink_available()
                last_uplink_refresh_at = loop_started

            current_uptime = uptime_seconds()
            current_time = time.time()
            temp = temperature_peak()
            fan_raw = duty_from_temp(temp, config)
            led_raw, led_sleeping, blink_active = effective_led_duty(config, current_uptime, cached_uplink, loop_started)
            manual = manual_override_values(config, current_time)
            if manual["fan_enabled"]:
                fan_raw = manual["fan_duty"]
            if manual["led_enabled"]:
                led_raw = manual["led_duty"]
            fan_signal_raw = apply_trigger_mode(fan_raw, bool(config.get("fan_active_low")))
            led_signal_raw = apply_trigger_mode(led_raw, bool(config.get("led_active_low")))
            fan_quantized = round(clamp_percent(fan_signal_raw), 1)
            led_quantized = round(clamp_percent(led_signal_raw), 1)
            send_legacy_4bit_frame(gpio, fan_signal_raw, led_signal_raw, config)

            state = {
                "timestamp": time.time(),
                "enabled": bool(config.get("enabled", True)),
                "chip": config["chip"],
                "line": int(config["line"]),
                "temp_c": round(temp, 1),
                "uptime_seconds": round(current_uptime, 1),
                "uplink": bool(cached_uplink),
                "connectivity": cached_connectivity,
                "fan_raw_duty": round(fan_raw, 1),
                "fan_duty": round(fan_raw, 1),
                "fan_signal_duty": fan_quantized,
                "fan_active_low": bool(config.get("fan_active_low")),
                "led_raw_duty": round(led_raw, 1),
                "led_duty": round(led_raw, 1),
                "led_signal_duty": led_quantized,
                "led_active_low": bool(config.get("led_active_low")),
                "led_sleeping": bool(led_sleeping),
                "blink_active": bool(blink_active),
                "frame_interval": float(config.get("frame_interval", 0.5)),
                "led_sleep_enabled": bool(config.get("led_sleep_enabled")),
                "led_sleep_after_minutes": float(config.get("led_sleep_after_minutes", 5.0)),
                "blink_on_disconnect": bool(config.get("blink_on_disconnect")),
                "blink_interval": float(config.get("blink_interval", 0.8)),
                "manual_active": bool(manual["active"]),
                "manual_remaining_seconds": float(manual["remaining_seconds"]),
                "manual_led_enabled": bool(manual["led_enabled"]),
                "manual_led_duty": round(manual["led_duty"], 1),
                "manual_fan_enabled": bool(manual["fan_enabled"]),
                "manual_fan_duty": round(manual["fan_duty"], 1),
                "protocol_magic": PROTOCOL_MAGIC,
                "protocol_magic_hex": f"0x{PROTOCOL_MAGIC:X}",
                "protocol_version": PROTOCOL_VERSION,
                "last_tx_seq": 0,
                "last_tx_type": last_tx_type,
                "legacy_compat": bool(legacy_compat),
                "ack_ok": False,
                "esp_ok": None,
                "esp_message": "离线单线发送中，不依赖 HTTP 在线确认",
                "esp_protocol_synced": False,
                "esp_protocol_magic": None,
                "esp_protocol_version": None,
                "esp_last_seq": 0,
                "esp_last_type": "legacy",
                "esp_rx_frames": 0,
            }
            try:
                write_state(state)
            except Exception:
                pass

            if loop_started - last_log_at >= max(1.0, float(config.get("log_interval", 30.0))):
                last_log_at = loop_started
                print(
                    f"temp={temp:.1f}C fan={fan_raw:.1f}%->{fan_quantized}% "
                    f"led={led_raw:.1f}%->{led_quantized}% "
                    f"uplink={'yes' if cached_uplink else 'no'} sleep={'yes' if led_sleeping else 'no'} "
                    f"blink={'yes' if blink_active else 'no'} "
                    f"manual={'yes' if manual['active'] else 'no'} "
                    f"tx=single_wire offline=yes "
                    f"fan_mode={'low' if config.get('fan_active_low') else 'high'} "
                    f"led_mode={'low' if config.get('led_active_low') else 'high'} line={config['line']}",
                    flush=True,
                )

            frame_interval = max(0.25, float(config.get("frame_interval", 1.0)))
            sleep_interruptible(max(0.0, frame_interval - (time.monotonic() - loop_started)))
        return 0
    finally:
        try:
            write_state(
                {
                    "timestamp": time.time(),
                    "enabled": False,
                    "error": "service_stopped",
                    "fan_duty": 0,
                    "led_duty": 0,
                    "line": int(config.get("line", 64)),
                }
            )
        except Exception:
            pass
        gpio.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
