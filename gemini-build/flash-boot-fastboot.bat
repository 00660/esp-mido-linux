@echo off
setlocal
chcp 65001 >nul

set "BOOT_IMG=%~1"
if "%BOOT_IMG%"=="" (
  echo 用法: flash-boot-fastboot.bat boot-xxxx.img
  exit /b 1
)

if not exist "%BOOT_IMG%" (
  echo 找不到镜像: %BOOT_IMG%
  exit /b 1
)

set "FASTBOOT=fastboot"
if exist "%~dp0..\platform-tools\fastboot.exe" set "FASTBOOT=%~dp0..\platform-tools\fastboot.exe"

echo 刷写 boot: %BOOT_IMG%
"%FASTBOOT%" flash boot "%BOOT_IMG%" || exit /b 1

echo 重启设备
"%FASTBOOT%" reboot
