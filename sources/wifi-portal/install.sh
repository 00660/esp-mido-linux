#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="/opt/mido-wifi-portal"
CONFIG_DIR="/etc/mido-wifi-portal"
SERVICE_PATH="/etc/systemd/system/mido-wifi-portal.service"

if [[ "${EUID}" -ne 0 ]]; then
	echo "请使用 root 运行 install.sh" >&2
	exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y python3 network-manager dnsmasq-base iw iputils-ping gpiod python3-libgpiod

install -d "${INSTALL_DIR}" "${CONFIG_DIR}"
install -m 0644 "${SCRIPT_DIR}/mido_wifi_portal.py" "${INSTALL_DIR}/mido_wifi_portal.py"
install -m 0644 "${SCRIPT_DIR}/portal.html" "${INSTALL_DIR}/portal.html"
install -m 0644 "${SCRIPT_DIR}/gpio64_led_pwm.py" "${INSTALL_DIR}/gpio64_led_pwm.py"
install -m 0644 "${SCRIPT_DIR}/mido_fan_control.py" "${INSTALL_DIR}/mido_fan_control.py"
install -m 0644 "${SCRIPT_DIR}/mido_esp_gpio_link.py" "${INSTALL_DIR}/mido_esp_gpio_link.py"
install -m 0644 "${SCRIPT_DIR}/mido-wifi-portal.service" "${SERVICE_PATH}"
install -m 0644 "${SCRIPT_DIR}/mido-gpio64-led.service" /etc/systemd/system/mido-gpio64-led.service
install -m 0644 "${SCRIPT_DIR}/mido-fan-control.service" /etc/systemd/system/mido-fan-control.service
install -m 0644 "${SCRIPT_DIR}/mido-esp-gpio-link.service" /etc/systemd/system/mido-esp-gpio-link.service

if [[ ! -f "${CONFIG_DIR}/config.json" ]]; then
	install -m 0644 "${SCRIPT_DIR}/config.example.json" "${CONFIG_DIR}/config.json"
fi
if [[ ! -f "${CONFIG_DIR}/fan-control.json" ]]; then
	install -m 0644 "${SCRIPT_DIR}/fan-control.example.json" "${CONFIG_DIR}/fan-control.json"
fi
if [[ ! -f "${CONFIG_DIR}/esp-gpio-link.json" ]]; then
	install -m 0644 "${SCRIPT_DIR}/esp-gpio-link.example.json" "${CONFIG_DIR}/esp-gpio-link.json"
fi

python3 -m py_compile "${INSTALL_DIR}/mido_wifi_portal.py"
python3 -m py_compile "${INSTALL_DIR}/gpio64_led_pwm.py"
python3 -m py_compile "${INSTALL_DIR}/mido_fan_control.py"
python3 -m py_compile "${INSTALL_DIR}/mido_esp_gpio_link.py"

systemctl daemon-reload
systemctl enable --now mido-wifi-portal.service
systemctl enable --now mido-gpio64-led.service
systemctl enable --now mido-esp-gpio-link.service

echo "mido-wifi-portal 安装完成"
