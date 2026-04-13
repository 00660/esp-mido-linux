#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="${GITHUB_WORKSPACE:-$(cd "$(dirname "$0")/.." && pwd)}"
WORK_DIR="${ROOT_DIR}/_work"
KERNEL_WORK_DIR="${WORK_DIR}/kernel"
ROOTFS_WORK_DIR="${WORK_DIR}/rootfs"
OUT_DIR="${ROOT_DIR}/out"
KERNEL_OUT_DIR="${OUT_DIR}/kernel"
FLASH_OUT_DIR="${OUT_DIR}/fullflash"
LINUX_DIR="${KERNEL_WORK_DIR}/linux"
KPL_DIR="${KERNEL_WORK_DIR}/KlipperPhonesLinux"
UNPACK_DIR="${ROOTFS_WORK_DIR}/unpacked"
CHROOT_DIR="${ROOTFS_WORK_DIR}/chroot"
ROOT_IMG_ZIP="${ROOTFS_WORK_DIR}/klipperos_base_rootfs.zip"
ROOT_IMG="${ROOTFS_WORK_DIR}/root.img"
BASE_ROOTFS_URL="${BASE_ROOTFS_URL:-https://github.com/umeiko/KlipperPhonesLinux/releases/download/base_rootfs/klipperos_base_rootfs.zip}"
PANEL_CMDLINE="${PANEL_CMDLINE:-mdss_mdp.panel=1:dsi:0:qcom,mdss_dsi_sharp_fhd_nt35695_cmd:1:none:cfg:single_dsi}"
KERNEL_LOCALVERSION="${KERNEL_LOCALVERSION:--gemini-gh}"

export ARCH=arm64
export CROSS_COMPILE=aarch64-linux-gnu-
export KBUILD_BUILD_USER=codex
export KBUILD_BUILD_HOST=github-actions

cleanup() {
  set +e
  if mountpoint -q "${CHROOT_DIR}/dev/pts"; then sudo umount "${CHROOT_DIR}/dev/pts"; fi
  if mountpoint -q "${CHROOT_DIR}/dev"; then sudo umount "${CHROOT_DIR}/dev"; fi
  if mountpoint -q "${CHROOT_DIR}/proc"; then sudo umount "${CHROOT_DIR}/proc"; fi
  if mountpoint -q "${CHROOT_DIR}/sys"; then sudo umount "${CHROOT_DIR}/sys"; fi
  if mountpoint -q "${CHROOT_DIR}"; then sudo umount "${CHROOT_DIR}"; fi
}

trap cleanup EXIT

mkdir -p "${KERNEL_WORK_DIR}" "${ROOTFS_WORK_DIR}" "${KERNEL_OUT_DIR}" "${FLASH_OUT_DIR}" "${CHROOT_DIR}"
rm -rf "${LINUX_DIR}" "${KPL_DIR}" "${UNPACK_DIR}"
rm -f "${ROOT_IMG_ZIP}" "${ROOT_IMG}"

git clone --depth 1 --branch "${KERNEL_TAG}" https://gitlab.com/msm8996-mainline/linux.git "${LINUX_DIR}"
git clone --depth 1 https://github.com/umeiko/KlipperPhonesLinux.git "${KPL_DIR}"

cp "${KPL_DIR}/LinuxKernels/msm8996/.config_gemini" "${LINUX_DIR}/.config"

# Fix a missing include in the panel driver on some msm8996 tags.
if [ -f "${LINUX_DIR}/drivers/gpu/drm/panel/panel-sony-synaptics-jdi.c" ] && \
   ! grep -q '^#include <linux/of.h>$' "${LINUX_DIR}/drivers/gpu/drm/panel/panel-sony-synaptics-jdi.c"; then
  sed -i '/^#include <linux\/of_platform.h>$/a #include <linux/of.h>' \
    "${LINUX_DIR}/drivers/gpu/drm/panel/panel-sony-synaptics-jdi.c"
fi

pushd "${LINUX_DIR}" >/dev/null

scripts/config --file .config --set-str LOCALVERSION "${KERNEL_LOCALVERSION}"
scripts/config --file .config --module NFT_COMPAT
scripts/config --file .config --module IP_NF_RAW
scripts/config --file .config --module IP6_NF_RAW
scripts/config --file .config --enable CGROUP_BPF
scripts/config --file .config --enable BPF_SYSCALL
scripts/config --file .config --enable SECCOMP
scripts/config --file .config --enable SECCOMP_FILTER
scripts/config --file .config --enable NF_NAT_REDIRECT
scripts/config --file .config --module NETFILTER_XT_MATCH_IPVS
scripts/config --file .config --enable BOOT_CONFIG
scripts/config --file .config --enable EXT2_FS
scripts/config --file .config --set-str SYSTEM_TRUSTED_KEYS ""
scripts/config --file .config --set-str SYSTEM_REVOCATION_KEYS ""

make olddefconfig

make -j"$(nproc)" Image.gz dtbs
make -j"$(nproc)" DEB_BUILD_PROFILES=pkg.linux-upstream.nokernelheaders bindeb-pkg

popd >/dev/null

find "${KERNEL_WORK_DIR}" -maxdepth 2 -type f -name "*.deb" -print -exec cp {} "${KERNEL_OUT_DIR}/" \;
cp "${LINUX_DIR}/.config" "${KERNEL_OUT_DIR}/config-gemini-final"
cp "${LINUX_DIR}/arch/arm64/boot/Image.gz" "${KERNEL_OUT_DIR}/"
find "${LINUX_DIR}/arch/arm64/boot/dts/qcom" -maxdepth 1 -type f -name "*gemini*.dtb" -exec cp {} "${KERNEL_OUT_DIR}/" \;

curl -L --retry 5 --retry-delay 5 --output "${ROOT_IMG_ZIP}" "${BASE_ROOTFS_URL}"
mkdir -p "${UNPACK_DIR}"
unzip -q "${ROOT_IMG_ZIP}" -d "${UNPACK_DIR}"

ROOT_IMG_SOURCE="$(find "${UNPACK_DIR}" -maxdepth 2 -type f -name "*.img" | head -n 1)"
if [ -z "${ROOT_IMG_SOURCE}" ]; then
  echo "No root image found in ${ROOT_IMG_ZIP}" >&2
  exit 1
fi
cp "${ROOT_IMG_SOURCE}" "${ROOT_IMG}"

sudo mount -o loop "${ROOT_IMG}" "${CHROOT_DIR}"
sudo mount --bind /proc "${CHROOT_DIR}/proc"
sudo mount --bind /dev "${CHROOT_DIR}/dev"
sudo mount --bind /dev/pts "${CHROOT_DIR}/dev/pts"
sudo mount --bind /sys "${CHROOT_DIR}/sys"
sudo cp /etc/resolv.conf "${CHROOT_DIR}/etc/resolv.conf"
sudo cp /etc/hosts "${CHROOT_DIR}/etc/hosts"
sudo mkdir -p "${CHROOT_DIR}/tmp/kernel"
sudo cp "${KERNEL_OUT_DIR}"/*.deb "${CHROOT_DIR}/tmp/kernel/"
QEMU_AARCH64="$(command -v qemu-aarch64 || true)"
if [ -z "${QEMU_AARCH64}" ]; then
  echo "qemu-aarch64 is not installed" >&2
  exit 1
fi

sudo proot -w / \
  -b /proc:/proc \
  -b /sys:/sys \
  -b /dev:/dev \
  -b /dev/pts:/dev/pts \
  -q "${QEMU_AARCH64}" \
  -r "${CHROOT_DIR}" \
  /bin/bash -c '
set -e
dpkg -l | grep -E "linux-headers|linux-image" | awk "{print \$2}" | xargs -r dpkg -P
rm -rf /lib/modules/*
dpkg -i /tmp/kernel/*.deb
'

sudo rsync -a "${KPL_DIR}/LinuxKernels/msm8996/firmware/" "${CHROOT_DIR}/lib/firmware/"
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

INITRD_IMG="$(find "${CHROOT_DIR}/boot" -maxdepth 1 -type f -name 'initrd.img-*' | head -n 1)"
if [ -z "${INITRD_IMG}" ]; then
  echo "Failed to locate initrd in chroot boot directory" >&2
  exit 1
fi
cp "${INITRD_IMG}" "${FLASH_OUT_DIR}/initrd.img"

DTB_PATH="$(find "${LINUX_DIR}/arch/arm64/boot/dts/qcom" -maxdepth 1 -type f -name '*gemini*.dtb' | head -n 1)"
if [ -z "${DTB_PATH}" ]; then
  echo "Failed to locate gemini dtb" >&2
  exit 1
fi

sudo sync
cleanup
trap cleanup EXIT

sudo e2label "${ROOT_IMG}" rootfs || true
ROOTFS_UUID="$(sudo blkid -s UUID -o value "${ROOT_IMG}")"
if [ -z "${ROOTFS_UUID}" ]; then
  echo "Failed to detect rootfs UUID" >&2
  exit 1
fi

cat "${LINUX_DIR}/arch/arm64/boot/Image.gz" "${DTB_PATH}" > "${FLASH_OUT_DIR}/kernel-dtb"
mkbootimg --base 0x80000000 \
  --kernel_offset 0x00008000 \
  --ramdisk_offset 0x01000000 \
  --tags_offset 0x00000100 \
  --pagesize 2048 \
  --second_offset 0x00f00000 \
  --ramdisk "${FLASH_OUT_DIR}/initrd.img" \
  --cmdline "console=tty0 root=UUID=${ROOTFS_UUID} rw loglevel=3 maxcpus=4 ${PANEL_CMDLINE}" \
  --kernel "${FLASH_OUT_DIR}/kernel-dtb" \
  -o "${FLASH_OUT_DIR}/boot.img"

rm -f "${FLASH_OUT_DIR}/kernel-dtb" "${FLASH_OUT_DIR}/initrd.img"
img2simg "${ROOT_IMG}" "${FLASH_OUT_DIR}/rootfs-simg.img"

cp "${ROOT_DIR}/gemini-build/flash-gemini-full-fastboot.bat" "${FLASH_OUT_DIR}/"
cp "${ROOT_DIR}/gemini-build/repack-android-boot.sh" "${FLASH_OUT_DIR}/"
cp "${ROOT_DIR}/gemini-build/install-on-device.sh" "${FLASH_OUT_DIR}/"

{
  echo "kernel_tag=${KERNEL_TAG}"
  echo "rootfs_uuid=${ROOTFS_UUID}"
  echo "base_config=umeiko/KlipperPhonesLinux LinuxKernels/msm8996/.config_gemini"
  echo "extra_config=NFT_COMPAT=m, IP_NF_RAW=m, IP6_NF_RAW=m, CGROUP_BPF=y, BPF_SYSCALL=y, SECCOMP=y, SECCOMP_FILTER=y, NF_NAT_REDIRECT=y, NETFILTER_XT_MATCH_IPVS=m, BOOT_CONFIG=y, EXT2_FS=y"
  echo "cmdline=console=tty0 root=UUID=${ROOTFS_UUID} rw loglevel=3 maxcpus=4 ${PANEL_CMDLINE}"
  echo "rootfs_partition=userdata"
} > "${FLASH_OUT_DIR}/build-info.txt"

(
  cd "${OUT_DIR}"
  sha256sum kernel/* fullflash/* > SHA256SUMS
)
