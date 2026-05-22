@echo off
:: Build mcp_bridge.gup for every 3ds Max version the local SDKs support
:: and stage each artifact in native/bin/ with the version suffix that
:: install.py looks for.
::
:: Requires:
::   - Visual Studio 2022 (Build Tools v143)
::   - CMake 3.20+
::   - The matching 3ds Max SDK installed at the default Autodesk path
::     for each version we attempt to build.
::
:: Usage:
::   build_all.bat              builds 2024, 2025, 2026, 2027 when SDKs found
::   build_all.bat 2026 2027    builds only the versions you name
setlocal enabledelayedexpansion

set NATIVE_DIR=%~dp0
set CMAKE="C:\Program Files\CMake\bin\cmake.exe"
set BIN_DIR=%NATIVE_DIR%bin

if not exist "%BIN_DIR%" mkdir "%BIN_DIR%"

if "%~1"=="" (
    set VERSIONS=2024 2025 2026 2027
) else (
    set VERSIONS=%*
)

set ANY_OK=0
set ANY_FAIL=0

for %%V in (%VERSIONS%) do (
    call :build %%V
)

echo.
echo === Summary ===
echo Built: %ANY_OK%   Failed: %ANY_FAIL%
if %ANY_FAIL% GTR 0 exit /b 1
exit /b 0

:build
set VER=%~1
set SDK_PATH=C:\Program Files\Autodesk\3ds Max %VER% SDK\maxsdk
if not exist "%SDK_PATH%\include\max.h" (
    echo.
    echo [%VER%] SKIP — no SDK at "%SDK_PATH%"
    goto :eof
)

echo.
echo [%VER%] Configuring...
set BUILD_DIR=%NATIVE_DIR%build_%VER%
%CMAKE% -B "%BUILD_DIR%" -G "Visual Studio 17 2022" -A x64 -DMAX_VERSION=%VER% "%NATIVE_DIR%"
if errorlevel 1 (
    echo [%VER%] CONFIGURE FAILED
    set /a ANY_FAIL=ANY_FAIL+1
    goto :eof
)

echo [%VER%] Building...
%CMAKE% --build "%BUILD_DIR%" --config Release
if errorlevel 1 (
    echo [%VER%] BUILD FAILED
    set /a ANY_FAIL=ANY_FAIL+1
    goto :eof
)

set GUP_SRC=%BUILD_DIR%\Release\mcp_bridge.gup
:: install.py expects 2027's binary at native/bin/mcp_bridge_2027.gup
:: and all other versions at native/bin/mcp_bridge.gup. Keep the suffix
:: for every non-default version so future installers can pick them up
:: without overwriting each other.
if "%VER%"=="2026" (
    set GUP_DST=%BIN_DIR%\mcp_bridge.gup
) else (
    set GUP_DST=%BIN_DIR%\mcp_bridge_%VER%.gup
)

copy /Y "%GUP_SRC%" "%GUP_DST%"
if errorlevel 1 (
    echo [%VER%] COPY FAILED — %GUP_SRC% -^> %GUP_DST%
    set /a ANY_FAIL=ANY_FAIL+1
    goto :eof
)

echo [%VER%] OK  →  %GUP_DST%
set /a ANY_OK=ANY_OK+1
goto :eof
