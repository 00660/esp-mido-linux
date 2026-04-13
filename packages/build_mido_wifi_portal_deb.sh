#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PKG_ROOT="${SCRIPT_DIR}/mido-wifi-portal_1.0.0_all"
OUT_DEB="${SCRIPT_DIR}/mido-wifi-portal_1.0.0_all.deb"

rm -rf "${PKG_ROOT}"
mkdir -p "${PKG_ROOT}/DEBIAN"
mkdir -p "${PKG_ROOT}/opt/mido-wifi-portal"
mkdir -p "${PKG_ROOT}/etc/mido-wifi-portal"
mkdir -p "${PKG_ROOT}/etc/systemd/system"

install -m 0644 "${SCRIPT_DIR}/../sources/wifi-portal/mido_wifi_portal.py" "${PKG_ROOT}/opt/mido-wifi-portal/mido_wifi_portal.py"
install -m 0644 "${SCRIPT_DIR}/../sources/wifi-portal/portal.html" "${PKG_ROOT}/opt/mido-wifi-portal/portal.html"
install -m 0644 "${SCRIPT_DIR}/../sources/wifi-portal/gpio64_led_pwm.py" "${PKG_ROOT}/opt/mido-wifi-portal/gpio64_led_pwm.py"
install -m 0644 "${SCRIPT_DIR}/../sources/wifi-portal/mido_fan_control.py" "${PKG_ROOT}/opt/mido-wifi-portal/mido_fan_control.py"
install -m 0644 "${SCRIPT_DIR}/../sources/wifi-portal/mido_esp_gpio_link.py" "${PKG_ROOT}/opt/mido-wifi-portal/mido_esp_gpio_link.py"

install -m 0644 "${SCRIPT_DIR}/../sources/wifi-portal/config.example.json" "${PKG_ROOT}/etc/mido-wifi-portal/config.json"
install -m 0644 "${SCRIPT_DIR}/../sources/wifi-portal/fan-control.example.json" "${PKG_ROOT}/etc/mido-wifi-portal/fan-control.json"
install -m 0644 "${SCRIPT_DIR}/../sources/wifi-portal/esp-gpio-link.example.json" "${PKG_ROOT}/etc/mido-wifi-portal/esp-gpio-link.json"

install -m 0644 "${SCRIPT_DIR}/../sources/wifi-portal/mido-wifi-portal.service" "${PKG_ROOT}/etc/systemd/system/mido-wifi-portal.service"
install -m 0644 "${SCRIPT_DIR}/../sources/wifi-portal/mido-gpio64-led.service" "${PKG_ROOT}/etc/systemd/system/mido-gpio64-led.service"
install -m 0644 "${SCRIPT_DIR}/../sources/wifi-portal/mido-fan-control.service" "${PKG_ROOT}/etc/systemd/system/mido-fan-control.service"
install -m 0644 "${SCRIPT_DIR}/../sources/wifi-portal/mido-esp-gpio-link.service" "${PKG_ROOT}/etc/systemd/system/mido-esp-gpio-link.service"

cat > "${PKG_ROOT}/DEBIAN/control" <<'EOF'
Package: mido-wifi-portal
Version: 1.0.0
Section: utils
Priority: optional
Architecture: all
Maintainer: 00660 <avu888@88.com>
Depends: python3, network-manager, dnsmasq-base, iw, iputils-ping, gpiod, python3-libgpiod
Description: Mido headless wifi portal and ESP single-wire control panel
EOF

cat > "${PKG_ROOT}/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
systemctl daemon-reload || true
systemctl enable mido-wifi-portal.service || true
systemctl enable mido-gpio64-led.service || true
systemctl enable mido-fan-control.service || true
systemctl enable mido-esp-gpio-link.service || true
systemctl restart mido-wifi-portal.service || true
systemctl restart mido-gpio64-led.service || true
systemctl restart mido-fan-control.service || true
systemctl restart mido-esp-gpio-link.service || true
exit 0
EOF

cat > "${PKG_ROOT}/DEBIAN/prerm" <<'EOF'
#!/bin/sh
set -e
systemctl stop mido-esp-gpio-link.service || true
systemctl stop mido-fan-control.service || true
systemctl stop mido-gpio64-led.service || true
systemctl stop mido-wifi-portal.service || true
exit 0
EOF

chmod 0755 "${PKG_ROOT}/DEBIAN/postinst" "${PKG_ROOT}/DEBIAN/prerm"
dpkg-deb --build "${PKG_ROOT}" "${OUT_DEB}"
echo "built: ${OUT_DEB}"
