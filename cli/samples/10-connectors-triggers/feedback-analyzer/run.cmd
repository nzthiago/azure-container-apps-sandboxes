@echo off
rem ============================================================================
rem  Windows wrapper for run.sh.
rem
rem  Why this exists: `bash` on a Windows PATH can resolve to either Git Bash
rem  (MSYS — works) or WSL bash (broken — its /tmp is invisible to the
rem  Windows-native `az.cmd` / `aca.exe` we shell out to, so JSON bodies
rem  passed via `@<file>` can't be read and ARM rejects the call). This
rem  wrapper finds Git Bash explicitly so users can just type
rem  `feedback-analyzer\run.cmd` from cmd or PowerShell.
rem ============================================================================
setlocal EnableExtensions
set "SCRIPT_DIR=%~dp0"
set "SH_FILE=%SCRIPT_DIR%run.sh"

set "GITBASH="
if exist "%ProgramFiles%\Git\bin\bash.exe"           set "GITBASH=%ProgramFiles%\Git\bin\bash.exe"
if not defined GITBASH if exist "%ProgramFiles(x86)%\Git\bin\bash.exe"     set "GITBASH=%ProgramFiles(x86)%\Git\bin\bash.exe"
if not defined GITBASH if exist "%LOCALAPPDATA%\Programs\Git\bin\bash.exe" set "GITBASH=%LOCALAPPDATA%\Programs\Git\bin\bash.exe"

rem Last resort: scan PATH for a bash.exe that isn't the WSL shim under
rem WindowsApps (those don't ship MSYS and they break our `@<file>` calls).
if not defined GITBASH (
    for /f "delims=" %%B in ('where bash.exe 2^>nul') do (
        echo %%B | findstr /I "\\WindowsApps\\" >nul
        if errorlevel 1 (
            set "GITBASH=%%B"
            goto :found
        )
    )
)
:found

if not defined GITBASH (
    echo error: Git Bash not found. Install "Git for Windows" from https://git-scm.com/download/win 1>^&2
    echo        ^(the WSL `bash` shim under %%LOCALAPPDATA%%\Microsoft\WindowsApps will not work^) 1>^&2
    exit /b 1
)

"%GITBASH%" "%SH_FILE%" %*
exit /b %ERRORLEVEL%
