@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
set "FASTBOOT=fastboot"

if exist "%SCRIPT_DIR%fastboot.exe" set "FASTBOOT=%SCRIPT_DIR%fastboot.exe"
if exist "%SCRIPT_DIR%..\flash-tools\fastboot.exe" set "FASTBOOT=%SCRIPT_DIR%..\flash-tools\fastboot.exe"

echo Flashing boot.img to boot...
"%FASTBOOT%" flash boot "%SCRIPT_DIR%boot.img"
if errorlevel 1 goto :fail

echo Flashing rootfs-simg.img to userdata...
"%FASTBOOT%" flash userdata "%SCRIPT_DIR%rootfs-simg.img"
if errorlevel 1 goto :fail

echo Rebooting device...
"%FASTBOOT%" reboot
if errorlevel 1 goto :fail

echo Flash complete.
exit /b 0

:fail
echo Flash failed.
exit /b 1
