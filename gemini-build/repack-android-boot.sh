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

cat "${VMLINUX}" "${DTB}" > "${WORK_DIR}/kernel-dtb"

mkbootimg \
  --base 0x80000000 \
  --kernel_offset 0x00008000 \
  --ramdisk_offset 0x01000000 \
  --tags_offset 0x00000100 \
  --pagesize 4096 \
  --second_offset 0x00f00000 \
  --kernel "${WORK_DIR}/kernel-dtb" \
  --ramdisk "${INITRD}" \
  --cmdline "${BOOT_CMDLINE}" \
  -o "${OUT_IMG}"

echo "created ${OUT_IMG}"
