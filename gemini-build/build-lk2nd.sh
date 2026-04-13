#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="${GITHUB_WORKSPACE:-$(cd "$(dirname "$0")/.." && pwd)}"
WORK_DIR="${ROOT_DIR}/_work/lk2nd"
OUT_DIR="${ROOT_DIR}/out/lk2nd"
LK2ND_SRC="${WORK_DIR}/lk2nd-src"
LK2ND_RELAXED="${WORK_DIR}/lk2nd-relaxed"

mkdir -p "${WORK_DIR}" "${OUT_DIR}"
rm -rf "${LK2ND_SRC}" "${LK2ND_RELAXED}"

git clone --depth 1 https://github.com/msm8916-mainline/lk2nd.git "${LK2ND_SRC}"
cp -a "${LK2ND_SRC}" "${LK2ND_RELAXED}"

pushd "${LK2ND_SRC}" >/dev/null
make TOOLCHAIN_PREFIX=arm-none-eabi- lk2nd-msm8996
cp build-lk2nd-msm8996/lk2nd.img "${OUT_DIR}/lk2nd-msm8996-official.img"
popd >/dev/null

cp "${ROOT_DIR}/gemini-build/lk2nd/msm8996-xiaomi-mi5-relaxed.dts" \
  "${LK2ND_RELAXED}/lk2nd/device/dts/msm8996/msm8996-xiaomi-mi5-relaxed.dts"

sed -i 's/msm8996-xiaomi-mi5\.dtb/msm8996-xiaomi-mi5-relaxed.dtb/' \
  "${LK2ND_RELAXED}/lk2nd/device/dts/msm8996/rules.mk"

pushd "${LK2ND_RELAXED}" >/dev/null
make TOOLCHAIN_PREFIX=arm-none-eabi- lk2nd-msm8996
cp build-lk2nd-msm8996/lk2nd.img "${OUT_DIR}/lk2nd-msm8996-gemini-relaxed.img"
popd >/dev/null

{
  echo "official=upstream msm8996-xiaomi-mi5.dts"
  echo "relaxed=gemini fallback without qcom,pmic-id match"
} > "${OUT_DIR}/variants.txt"
