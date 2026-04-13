#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "please run as root" >&2
  exit 1
fi

PKG_DIR="${1:-$PWD}"
OUT_DIR="${2:-$PWD/out}"

if [[ ! -d "${PKG_DIR}" ]]; then
  echo "package dir not found: ${PKG_DIR}" >&2
  exit 1
fi

mkdir -p "${OUT_DIR}"

if ! command -v mkbootimg >/dev/null 2>&1; then
  apt-get update
  apt-get install -y mkbootimg
fi

mapfile -t PKGS < <(find "${PKG_DIR}" -maxdepth 1 -type f \( -name "linux-image-*.deb" -o -name "linux-headers-*.deb" \) ! -name "*dbg*.deb" | sort)

if [[ ${#PKGS[@]} -eq 0 ]]; then
  echo "no linux deb packages found in ${PKG_DIR}" >&2
  exit 1
fi

dpkg -i "${PKGS[@]}"

LATEST_IMAGE_DEB="$(basename "$(printf '%s\n' "${PKGS[@]}" | grep '/linux-image-' | tail -n 1)")"
KVER="${LATEST_IMAGE_DEB#linux-image-}"
KVER="${KVER%_*}"

if [[ ! -f "/boot/initrd.img-${KVER}" ]]; then
  update-initramfs -c -k "${KVER}"
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
"${SCRIPT_DIR}/repack-android-boot.sh" "${KVER}"

mv -f "${PWD}/boot-${KVER}.img" "${OUT_DIR}/boot-${KVER}.img"

echo "kernel version: ${KVER}"
echo "boot image: ${OUT_DIR}/boot-${KVER}.img"
