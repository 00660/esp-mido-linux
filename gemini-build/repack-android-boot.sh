#!/usr/bin/env bash
set -euo pipefail

KVER="${1:-$(basename "$(readlink -f /vmlinuz)" | sed 's/^vmlinuz-//')}"
ROOTFS_UUID="${ROOTFS_UUID:-$(findmnt -n -o UUID /)}"
BOOT_CMDLINE="${BOOT_CMDLINE:-console=tty0 root=UUID=${ROOTFS_UUID} rw loglevel=3 maxcpus=4 mdss_mdp.panel=1:dsi:0:qcom,mdss_dsi_sharp_fhd_nt35695_cmd:1:none:cfg:single_dsi}"

VMLINUX="/boot/vmlinuz-${KVER}"
INITRD="/boot/initrd.img-${KVER}"
DTB="$(find "/usr/lib/linux-image-${KVER}/qcom" -maxdepth 1 -type f -name "*gemini*.dtb" | sort | head -n 1)"
OUT_IMG="${PWD}/boot-${KVER}.img"
WORK_DIR="$(mktemp -d)"
MKBOOTIMG_PY="${WORK_DIR}/mkbootimg.py"

cleanup() {
  rm -rf "${WORK_DIR}"
}
trap cleanup EXIT

if [[ ! -f "${VMLINUX}" ]]; then
  echo "missing ${VMLINUX}" >&2
  exit 1
fi

if [[ ! -f "${INITRD}" ]]; then
  echo "missing ${INITRD}" >&2
  exit 1
fi

if [[ -z "${DTB}" || ! -f "${DTB}" ]]; then
  echo "missing gemini dtb under /usr/lib/linux-image-${KVER}/qcom" >&2
  exit 1
fi

curl -L --retry 5 --retry-delay 5 \
  --output "${MKBOOTIMG_PY}" \
  "https://sources.debian.org/data/main/a/android-platform-tools/34.0.5-12/system/tools/mkbootimg/mkbootimg.py"

cat "${VMLINUX}" "${DTB}" > "${WORK_DIR}/kernel-dtb"

python3 "${MKBOOTIMG_PY}" \
  --base 0x80000000 \
  --kernel_offset 0x00008000 \
  --ramdisk_offset 0x01000000 \
  --tags_offset 0x00000100 \
  --pagesize 2048 \
  --second_offset 0x00f00000 \
  --kernel "${WORK_DIR}/kernel-dtb" \
  --ramdisk "${INITRD}" \
  --cmdline "${BOOT_CMDLINE}" \
  -o "${OUT_IMG}"

if [[ ! -s "${OUT_IMG}" ]]; then
  echo "failed to create ${OUT_IMG}" >&2
  exit 1
fi

echo "created ${OUT_IMG}"
