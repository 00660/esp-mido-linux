# gemini-msm8996

This branch is only for GitHub Actions builds.

Artifacts:

- `out/kernel/deb/*`: arm64 kernel debs built from `v6.19.5-msm8996` by default.
- `out/kernel/image/*`: raw `Image.gz`, final `.config`, and `gemini` dtb files.
- `out/kernel/helper/repack-android-boot.sh`: repack helper for the existing Android boot flow.
- `out/lk2nd/lk2nd-msm8996-official.img`: upstream `gemini` lk2nd build.
- `out/lk2nd/lk2nd-msm8996-gemini-relaxed.img`: fallback lk2nd build with relaxed `gemini` match.

Kernel config base comes from `umeiko/KlipperPhonesLinux`:

- `LinuxKernels/msm8996/.config_gemini`

Extra options forced on top:

- `CONFIG_NFT_COMPAT=m`
- `CONFIG_IP_NF_RAW=m`
- `CONFIG_BOOT_CONFIG=y`
- `CONFIG_EXT2_FS=y`
