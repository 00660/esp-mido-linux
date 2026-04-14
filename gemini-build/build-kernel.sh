#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="${GITHUB_WORKSPACE:-$(cd "$(dirname "$0")/.." && pwd)}"
WORK_DIR="${ROOT_DIR}/_work"
KPL_DIR="${WORK_DIR}/KlipperPhonesLinux"
SCRIPT_DIR="${KPL_DIR}/LinuxKernels/scripts"
LINUX_DIR="${SCRIPT_DIR}/linux"
ROOTFS_WORK_DIR="${WORK_DIR}/rootfs"
UNPACK_DIR="${ROOTFS_WORK_DIR}/unpacked"
ROOT_IMG_ZIP="${ROOTFS_WORK_DIR}/klipperos_base_rootfs.zip"
ROOT_IMG="${SCRIPT_DIR}/root.img"
OUT_DIR="${ROOT_DIR}/out"
KERNEL_OUT_DIR="${OUT_DIR}/kernel"
FLASH_OUT_DIR="${OUT_DIR}/fullflash"
TMP_MKBOOT_DIR="${SCRIPT_DIR}/tmp_mkboot"
CHROOT_DIR="/mnt/chroot"
BASE_ROOTFS_URL="${BASE_ROOTFS_URL:-https://github.com/umeiko/KlipperPhonesLinux/releases/download/base_rootfs/klipperos_base_rootfs.zip}"
KERNEL_TAG="${KERNEL_TAG:-v6.1.14-msm8996}"
QEMU_STATIC="${QEMU_STATIC:-/usr/bin/qemu-aarch64-static}"
RUNNING_CONFIG="${ROOT_DIR}/gemini-build/config-gemini-running-6.1.14-umeko-rv0"
FIRMWARE_OVERLAY_DIR="${ROOT_DIR}/gemini-build/firmware-overlay"

export ARCH=arm64
export CROSS_COMPILE=aarch64-linux-gnu-
export CC=aarch64-linux-gnu-gcc
export DEBIAN_FRONTEND=noninteractive
export KBUILD_BUILD_USER=codex
export KBUILD_BUILD_HOST=github-actions

cleanup() {
  set +e
  sync
  if mountpoint -q "${CHROOT_DIR}/dev/pts"; then sudo umount "${CHROOT_DIR}/dev/pts"; fi
  if mountpoint -q "${CHROOT_DIR}/dev"; then sudo umount "${CHROOT_DIR}/dev"; fi
  if mountpoint -q "${CHROOT_DIR}/proc"; then sudo umount "${CHROOT_DIR}/proc"; fi
  if mountpoint -q "${CHROOT_DIR}/sys"; then sudo umount "${CHROOT_DIR}/sys"; fi
  if mountpoint -q "${CHROOT_DIR}"; then sudo umount "${CHROOT_DIR}"; fi
}

trap cleanup EXIT

rm -rf "${KPL_DIR}" "${ROOTFS_WORK_DIR}" "${OUT_DIR}"
mkdir -p "${WORK_DIR}" "${ROOTFS_WORK_DIR}" "${UNPACK_DIR}" "${KERNEL_OUT_DIR}" "${FLASH_OUT_DIR}"
sudo mkdir -p "${CHROOT_DIR}"

git clone --depth 1 https://github.com/umeiko/KlipperPhonesLinux.git "${KPL_DIR}"

pushd "${SCRIPT_DIR}" >/dev/null

git clone --depth 1 --branch "${KERNEL_TAG}" https://gitlab.com/msm8996-mainline/linux.git ./linux
if [ -f "${RUNNING_CONFIG}" ]; then
  cp "${RUNNING_CONFIG}" ./linux/.config
else
  cp ../msm8996/.config_gemini ./linux/.config
fi

pushd ./linux >/dev/null
if [ -f ./drivers/gpu/drm/panel/panel-sony-synaptics-jdi.c ] && \
   ! grep -q '^#include <linux/of.h>$' ./drivers/gpu/drm/panel/panel-sony-synaptics-jdi.c; then
  sed -i '/^#include <linux\/of_platform.h>$/a #include <linux/of.h>' \
    ./drivers/gpu/drm/panel/panel-sony-synaptics-jdi.c
fi
make olddefconfig
popd >/dev/null

bash ./full_compile.sh

find . -maxdepth 1 -type f -name "*.deb" -exec cp {} "${KERNEL_OUT_DIR}/" \;
cp ./linux/.config "${KERNEL_OUT_DIR}/config-gemini-final"
KERNEL_IMAGE_PATH="./linux/arch/arm64/boot/Image.gz"
if [ ! -f "${KERNEL_IMAGE_PATH}" ]; then
  KERNEL_IMAGE_PATH="./linux/arch/arm64/boot/Image"
fi
if [ ! -f "${KERNEL_IMAGE_PATH}" ]; then
  echo "Failed to locate built kernel image" >&2
  exit 1
fi
cp "${KERNEL_IMAGE_PATH}" "${KERNEL_OUT_DIR}/"
find ./linux/arch/arm64/boot/dts/qcom -maxdepth 1 -type f -name "*gemini*.dtb" -exec cp {} "${KERNEL_OUT_DIR}/" \;

curl -L --retry 5 --retry-delay 5 --output "${ROOT_IMG_ZIP}" "${BASE_ROOTFS_URL}"
unzip -q "${ROOT_IMG_ZIP}" -d "${UNPACK_DIR}"

ROOT_IMG_SOURCE="$(find "${UNPACK_DIR}" -maxdepth 2 -type f -name "*.img" | head -n 1)"
if [ -z "${ROOT_IMG_SOURCE}" ]; then
  echo "No root image found in ${ROOT_IMG_ZIP}" >&2
  exit 1
fi

rm -f "${ROOT_IMG}"
cp "${ROOT_IMG_SOURCE}" "${ROOT_IMG}"

sudo mount -o loop "${ROOT_IMG}" "${CHROOT_DIR}"
sudo mount --bind /proc "${CHROOT_DIR}/proc"
sudo mount --bind /dev "${CHROOT_DIR}/dev"
sudo mount --bind /dev/pts "${CHROOT_DIR}/dev/pts"
sudo mount --bind /sys "${CHROOT_DIR}/sys"
sudo cp /etc/resolv.conf "${CHROOT_DIR}/etc/resolv.conf"

sudo cp ./*.deb "${CHROOT_DIR}/tmp/"
sudo cp ./chroot_install_kernel.sh "${CHROOT_DIR}/tmp/install_kernel.sh"
sudo chmod +x "${CHROOT_DIR}/tmp/install_kernel.sh"

if [ ! -x "${QEMU_STATIC}" ]; then
  echo "qemu-aarch64-static is not installed" >&2
  exit 1
fi

sudo cp "${QEMU_STATIC}" "${CHROOT_DIR}/usr/bin/qemu-aarch64-static"
sudo chroot "${CHROOT_DIR}" /usr/bin/qemu-aarch64-static /bin/bash -lc \
  "export DEBIAN_FRONTEND=noninteractive; cd /tmp; /bin/bash ./install_kernel.sh"

sudo rsync -a ../msm8996/firmware/ "${CHROOT_DIR}/lib/firmware/"
if [ -d "${FIRMWARE_OVERLAY_DIR}" ]; then
  sudo rsync -a "${FIRMWARE_OVERLAY_DIR}/" "${CHROOT_DIR}/lib/firmware/"
fi
sudo rm -f "${CHROOT_DIR}/lib/firmware/qcom/msm8996/gemini/adsp.mbn"
sudo mkdir -p "${CHROOT_DIR}/etc/modprobe.d"
sudo tee "${CHROOT_DIR}/etc/modprobe.d/msm8996-network-order.conf" >/dev/null <<'EOF'
softdep drm pre: panel_jdi_fhd_r63452
softdep panel_jdi_fhd_r63452 pre: rmtfs_mem
softdep ath pre: ath10k_core
softdep ath10k_core pre: ath10k_pci
softdep ath10k_pci pre: cfg80211
softdep cfg80211 pre: mac80211
softdep mac80211 pre: rmtfs_mem
EOF

if [ -f "${CHROOT_DIR}/home/auto_resize_script.sh" ] && \
   ! grep -q '/sys/class/pci_bus/0000\\:01/rescan' "${CHROOT_DIR}/home/auto_resize_script.sh"; then
  sudo tee -a "${CHROOT_DIR}/home/auto_resize_script.sh" >/dev/null <<'EOF'
sleep 30
echo 1 > /sys/class/pci_bus/0000\:01/rescan
EOF
fi

ROOTFS_UUID="$(sudo blkid -s UUID -o value "${ROOT_IMG}")"
if [ -z "${ROOTFS_UUID}" ]; then
  echo "Failed to detect rootfs UUID" >&2
  exit 1
fi

cat > ./get_kernel_files.sh <<'EOF'
mkdir ./tmp_mkboot
rm -rf ./tmp_mkboot/*
cp ./linux/arch/arm64/boot/dts/qcom/*gemini*.dtb ./tmp_mkboot/
if [ -f ./linux/arch/arm64/boot/Image.gz ]; then
  cp ./linux/arch/arm64/boot/Image.gz ./tmp_mkboot/kernel.img
else
  cp ./linux/arch/arm64/boot/Image ./tmp_mkboot/kernel.img
fi
cp /mnt/chroot/boot/initrd* ./tmp_mkboot/
EOF

cat > ./mkboot.sh <<EOF
cp ./tmp_mkboot/initrd* ./tmp_mkboot/initrd.img
cp ./tmp_mkboot/*gemini*.dtb ./tmp_mkboot/dtb
cat ./tmp_mkboot/kernel.img ./tmp_mkboot/dtb > ./tmp_mkboot/kernel-dtb
mkbootimg --base 0x80000000 \\
        --kernel_offset 0x00008000 \\
        --ramdisk_offset 0x01000000 \\
        --tags_offset 0x00000100 \\
        --pagesize 2048 \\
        --second_offset 0x00f00000 \\
        --ramdisk ./tmp_mkboot/initrd.img \\
        --cmdline "console=tty0 root=UUID=${ROOTFS_UUID} rw loglevel=3 splash" \\
        --kernel ./tmp_mkboot/kernel-dtb -o ./tmp_mkboot/boot.img
rm ./tmp_mkboot/dtb
rm ./tmp_mkboot/kernel-dtb
rm ./tmp_mkboot/kernel.img
rm ./tmp_mkboot/initrd.img
rm -f ./tmp_mkboot/rootfs.img
img2simg ./root.img ./tmp_mkboot/rootfs.img
EOF

chmod +x ./get_kernel_files.sh ./mkboot.sh
sync
bash ./get_kernel_files.sh
bash ./mkboot.sh

cp "${TMP_MKBOOT_DIR}/boot.img" "${FLASH_OUT_DIR}/"
cp "${TMP_MKBOOT_DIR}/rootfs.img" "${FLASH_OUT_DIR}/rootfs.img"
cp "${TMP_MKBOOT_DIR}/rootfs.img" "${FLASH_OUT_DIR}/rootfs-simg.img"
cp "${ROOT_DIR}/gemini-build/flash-gemini-full-fastboot.bat" "${FLASH_OUT_DIR}/"
cp "${ROOT_DIR}/gemini-build/repack-android-boot.sh" "${FLASH_OUT_DIR}/"
cp "${ROOT_DIR}/gemini-build/install-on-device.sh" "${FLASH_OUT_DIR}/"

{
  echo "kernel_tag=${KERNEL_TAG}"
  echo "rootfs_uuid=${ROOTFS_UUID}"
  if [ -f "${RUNNING_CONFIG}" ]; then
    echo "base_config=gemini-build/config-gemini-running-6.1.14-umeko-rv0"
  else
    echo "base_config=umeiko/KlipperPhonesLinux LinuxKernels/msm8996/.config_gemini"
  fi
  if [ -d "${FIRMWARE_OVERLAY_DIR}" ]; then
    echo "firmware_overlay=gemini-build/firmware-overlay"
  fi
  echo "build_flow=umeiko tutorial chain"
  echo "cmdline=console=tty0 root=UUID=${ROOTFS_UUID} rw loglevel=3 splash"
} > "${FLASH_OUT_DIR}/build-info.txt"

(
  cd "${OUT_DIR}"
  sha256sum kernel/* fullflash/* > SHA256SUMS
)

popd >/dev/null
