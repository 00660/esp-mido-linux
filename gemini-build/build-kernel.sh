#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="${GITHUB_WORKSPACE:-$(cd "$(dirname "$0")/.." && pwd)}"
WORK_DIR="${ROOT_DIR}/_work/kernel"
OUT_DIR="${ROOT_DIR}/out/kernel"
LINUX_DIR="${WORK_DIR}/linux"
KPL_DIR="${WORK_DIR}/KlipperPhonesLinux"

export ARCH=arm64
export CROSS_COMPILE=aarch64-linux-gnu-
export KBUILD_BUILD_USER=codex
export KBUILD_BUILD_HOST=github-actions

mkdir -p "${WORK_DIR}" "${OUT_DIR}"
rm -rf "${LINUX_DIR}" "${KPL_DIR}"

git clone --depth 1 --branch "${KERNEL_TAG}" https://gitlab.com/msm8996-mainline/linux.git "${LINUX_DIR}"
git clone --depth 1 https://github.com/umeiko/KlipperPhonesLinux.git "${KPL_DIR}"

cp "${KPL_DIR}/LinuxKernels/msm8996/.config_gemini" "${LINUX_DIR}/.config"

# Fix missing declaration of of_device_get_match_data() in this tag.
if ! grep -q '^#include <linux/of.h>$' "${LINUX_DIR}/drivers/gpu/drm/panel/panel-sony-synaptics-jdi.c"; then
  sed -i '/^#include <linux\/of_platform.h>$/a #include <linux/of.h>' \
    "${LINUX_DIR}/drivers/gpu/drm/panel/panel-sony-synaptics-jdi.c"
fi

pushd "${LINUX_DIR}" >/dev/null

scripts/config --file .config --set-str LOCALVERSION "-gemini-gh"
scripts/config --file .config --module NFT_COMPAT
scripts/config --file .config --module IP_NF_RAW
scripts/config --file .config --enable BOOT_CONFIG
scripts/config --file .config --enable EXT2_FS
scripts/config --file .config --set-str SYSTEM_TRUSTED_KEYS ""
scripts/config --file .config --set-str SYSTEM_REVOCATION_KEYS ""

make olddefconfig

make -j"$(nproc)" Image.gz dtbs
mkdir -p "${OUT_DIR}/deb" "${OUT_DIR}/image" "${OUT_DIR}/helper"

cp "${LINUX_DIR}/.config" "${OUT_DIR}/image/config-gemini-final"
cp "${LINUX_DIR}/arch/arm64/boot/Image.gz" "${OUT_DIR}/image/"
find "${LINUX_DIR}/arch/arm64/boot/dts/qcom" -maxdepth 1 -type f -name "*gemini*.dtb" -exec cp {} "${OUT_DIR}/image/" \;
cp "${ROOT_DIR}/gemini-build/repack-android-boot.sh" "${OUT_DIR}/helper/"
cp "${ROOT_DIR}/gemini-build/install-on-device.sh" "${OUT_DIR}/helper/"
cp "${ROOT_DIR}/gemini-build/flash-boot-fastboot.bat" "${OUT_DIR}/helper/"

make -j"$(nproc)" DEB_BUILD_PROFILES=pkg.linux-upstream.nokernelheaders bindeb-pkg

popd >/dev/null

find "${WORK_DIR}" -maxdepth 2 -type f -name "*.deb" -print -exec cp {} "${OUT_DIR}/deb/" \;

{
  echo "kernel_tag=${KERNEL_TAG}"
  echo "rootfs_uuid=${ROOTFS_UUID:-}"
  echo "base_config=umeiko/KlipperPhonesLinux LinuxKernels/msm8996/.config_gemini"
  echo "extra_config=NFT_COMPAT=m, IP_NF_RAW=m, BOOT_CONFIG=y, EXT2_FS=y"
} > "${OUT_DIR}/build-info.txt"
