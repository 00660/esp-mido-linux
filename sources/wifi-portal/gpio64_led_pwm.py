#!/usr/bin/env python3
import argparse
import signal
import sys
import time


STOP = False


def parse_args():
    parser = argparse.ArgumentParser(description="GPIO64 指示灯软件 PWM")
    parser.add_argument("--chip", default="/dev/gpiochip0", help="gpiochip 路径或名称，默认 /dev/gpiochip0")
    parser.add_argument("--line", type=int, default=64, help="GPIO line offset，默认 64")
    parser.add_argument("--duty", type=float, default=30.0, help="占空比百分比，默认 30")
    parser.add_argument("--hz", type=float, default=100.0, help="PWM 频率，默认 100Hz")
    parser.add_argument("--auto-off-seconds", type=float, default=0.0, help="自动熄灭秒数，0 表示常亮")
    parser.add_argument("--consumer", default="mido-gpio64-led", help="GPIO consumer 名称")
    parser.add_argument("--active-low", action="store_true", help="反相输出，适用于低电平点亮")
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


def main():
    args = parse_args()
    duty = max(0.0, min(100.0, float(args.duty)))
    frequency = max(1.0, float(args.hz))
    auto_off_seconds = max(0.0, float(args.auto_off_seconds))
    period = 1.0 / frequency
    active_time = period * (duty / 100.0)
    inactive_time = max(0.0, period - active_time)

    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)

    gpio = GpioLine(args.chip, args.line, args.consumer)
    active_level = not bool(args.active_low)
    deadline = time.monotonic() + auto_off_seconds if auto_off_seconds > 0 else None

    try:
        if duty <= 0.0:
            gpio.set_value(not active_level)
            while not STOP:
                if deadline is not None and time.monotonic() >= deadline:
                    break
                time.sleep(1.0)
            return 0

        if duty >= 100.0:
            gpio.set_value(active_level)
            while not STOP:
                if deadline is not None and time.monotonic() >= deadline:
                    gpio.set_value(not active_level)
                    break
                time.sleep(1.0)
            return 0

        while not STOP:
            if deadline is not None and time.monotonic() >= deadline:
                gpio.set_value(not active_level)
                break
            gpio.set_value(active_level)
            time.sleep(active_time)
            if STOP:
                break
            if deadline is not None and time.monotonic() >= deadline:
                gpio.set_value(not active_level)
                break
            gpio.set_value(not active_level)
            time.sleep(inactive_time)
        return 0
    finally:
        gpio.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
