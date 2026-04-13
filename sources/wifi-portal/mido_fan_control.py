#!/usr/bin/env python3
import argparse
import json
import signal
import sys
import time
from pathlib import Path


STOP = False
DEFAULT_CONFIG = {
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


def parse_args():
    parser = argparse.ArgumentParser(description="Mido 温控风扇服务")
    parser.add_argument("--config", default="/etc/mido-wifi-portal/fan-control.json", help="配置文件路径")
    parser.add_argument("--consumer", default="mido-fan-control", help="GPIO consumer 名称")
    return parser.parse_args()


def stop_handler(signum, frame):
    del signum, frame
    global STOP
    STOP = True


class GpioLine:
    def __init__(self, chip_name, offset, consumer):
        self.offset = int(offset)
        self.consumer = consumer
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
                output_value=line_module.Value.INACTIVE,
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
        self.line.request(consumer=self.consumer, type=self.gpiod.LINE_REQ_DIR_OUT, default_vals=[0])
        self.backend = "v1"

    def set_value(self, active):
        value = 1 if active else 0
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
            self.set_value(False)
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


def duty_from_temp(temp, config):
    if not config.get("enabled", True):
        return 0.0
    start_temp = float(config["start_temp"])
    full_temp = float(config["full_temp"])
    min_duty = float(config["min_duty"])
    max_duty = float(config["max_duty"])

    if temp < start_temp:
        return 0.0
    if temp >= full_temp:
        return max_duty
    ratio = (temp - start_temp) / max(full_temp - start_temp, 0.1)
    return min_duty + ratio * (max_duty - min_duty)


def sleep_until(deadline):
    while not STOP:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 0.25))


def main():
    args = parse_args()
    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)

    config = load_config(args.config)
    gpio = GpioLine(config["chip"], int(config["line"]), args.consumer)
    active_level = not bool(config.get("active_low"))

    try:
        while not STOP:
            config = load_config(args.config)
            duty = max(0.0, min(100.0, float(duty_from_temp(temperature_peak(), config))))
            hz = max(1.0, float(config["hz"]))
            poll_interval = max(0.2, float(config["poll_interval"]))
            period = 1.0 / hz
            deadline = time.monotonic() + poll_interval

            if duty <= 0.0:
                gpio.set_value(not active_level)
                sleep_until(deadline)
                continue

            if duty >= 100.0:
                gpio.set_value(active_level)
                sleep_until(deadline)
                continue

            active_time = period * (duty / 100.0)
            inactive_time = max(0.0, period - active_time)
            while not STOP and time.monotonic() < deadline:
                gpio.set_value(active_level)
                time.sleep(min(active_time, max(deadline - time.monotonic(), 0.0)))
                if STOP or time.monotonic() >= deadline:
                    break
                gpio.set_value(not active_level)
                time.sleep(min(inactive_time, max(deadline - time.monotonic(), 0.0)))
        return 0
    finally:
        gpio.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
