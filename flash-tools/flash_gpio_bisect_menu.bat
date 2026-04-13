@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul

set "SCRIPT_DIR=%~dp0"
set "FASTBOOT=fastboot"
if exist "%SCRIPT_DIR%fastboot.exe" set "FASTBOOT=%SCRIPT_DIR%fastboot.exe"
if exist "%SCRIPT_DIR%..\platform-tools\fastboot.exe" set "FASTBOOT=%SCRIPT_DIR%..\platform-tools\fastboot.exe"

echo mido GPIO bisect flash menu
echo.
echo Variants:
echo   1. pshold-only
echo   2. sidekeys-only
echo   3. touch-only
echo   4. display-only
echo   5. ir-only
echo   6. reserved-only
echo   7. all-groups
echo   8. groups-1-5-no-reserved
echo.
set /p CHOICE=Select variant number: 

if "%CHOICE%"=="1" set "BOOTFS=01-pshold-only-bootfs-simg.img"
if "%CHOICE%"=="2" set "BOOTFS=02-sidekeys-only-bootfs-simg.img"
if "%CHOICE%"=="3" set "BOOTFS=03-touch-only-bootfs-simg.img"
if "%CHOICE%"=="4" set "BOOTFS=04-display-only-bootfs-simg.img"
if "%CHOICE%"=="5" set "BOOTFS=05-ir-only-bootfs-simg.img"
if "%CHOICE%"=="6" set "BOOTFS=06-reserved-only-bootfs-simg.img"
if "%CHOICE%"=="7" set "BOOTFS=07-all-groups-bootfs-simg.img"
if "%CHOICE%"=="8" set "BOOTFS=08-all-except-reserved-bootfs-simg.img"

if not defined BOOTFS (
  echo.
  echo Invalid variant.
  pause
  exit /b 1
)

echo.
echo Flash modes:
echo   1. system only ^(recommended for bisect^)
echo   2. lk2nd + system
echo   3. lk2nd + system + userdata/rootfs
echo.
set /p MODE=Select flash mode: 

if "%MODE%"=="1" goto :flash_system
if "%MODE%"=="2" goto :flash_lk_system
if "%MODE%"=="3" goto :flash_full

echo.
echo Invalid mode.
pause
exit /b 1

:flash_system
echo.
echo Flashing system with %BOOTFS%
"%FASTBOOT%" flash system "%SCRIPT_DIR%%BOOTFS%"
if errorlevel 1 goto :fail
goto :reboot_only

:flash_lk_system
echo.
echo Flashing lk2nd...
"%FASTBOOT%" flash boot "%SCRIPT_DIR%lk2nd.img"
if errorlevel 1 goto :fail
echo Rebooting into lk2nd fastboot...
"%FASTBOOT%" reboot
if errorlevel 1 goto :fail
timeout /t 10 >nul
echo Flashing system with %BOOTFS%
"%FASTBOOT%" flash system "%SCRIPT_DIR%%BOOTFS%"
if errorlevel 1 goto :fail
goto :reboot_only

:flash_full
echo.
echo Erasing boot/system/userdata...
"%FASTBOOT%" erase boot
if errorlevel 1 goto :fail
"%FASTBOOT%" erase system
if errorlevel 1 goto :fail
"%FASTBOOT%" erase userdata
if errorlevel 1 goto :fail
echo Flashing lk2nd...
"%FASTBOOT%" flash boot "%SCRIPT_DIR%lk2nd.img"
if errorlevel 1 goto :fail
echo Rebooting into lk2nd fastboot...
"%FASTBOOT%" reboot
if errorlevel 1 goto :fail
timeout /t 10 >nul
echo Flashing system with %BOOTFS%
"%FASTBOOT%" flash system "%SCRIPT_DIR%%BOOTFS%"
if errorlevel 1 goto :fail
echo Flashing userdata/rootfs...
"%FASTBOOT%" flash userdata "%SCRIPT_DIR%rootfs-simg.img"
if errorlevel 1 goto :fail
goto :reboot_only

:reboot_only
echo.
echo Rebooting device...
"%FASTBOOT%" reboot
if errorlevel 1 goto :fail
echo.
echo Done.
pause
exit /b 0

:fail
echo.
echo Flash failed.
pause
exit /b 1
