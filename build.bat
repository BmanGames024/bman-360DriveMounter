@echo off
setlocal
cd /d "%~dp0"

rem ==== Name of the exe that ends up in the dist folder ====
set "NAME=360DriveMounter"

rem ---- Find Python (the py launcher first, then python on PATH) ----
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY set "PY=python"
%PY% --version >nul 2>nul
if errorlevel 1 (
    echo Python was not found. Install Python 3 from python.org and tick "Add python.exe to PATH".
    goto :fail
)
for /f "delims=" %%v in ('%PY% --version') do echo Using %%v

rem ---- Build tools and the app's own requirements ----
echo.
echo Installing / updating PyInstaller and requirements...
%PY% -m pip install --upgrade pip pyinstaller -r requirements.txt
if errorlevel 1 goto :fail

rem ---- Files bundled inside the exe (app.py looks for them next to itself) ----
set "DATA=--add-data logo.ico;. --add-data logo.png;."
if exist winfsp.msi (
    set "DATA=%DATA% --add-data winfsp.msi;."
) else (
    echo WARNING: winfsp.msi not found - the exe won't be able to install WinFsp by itself.
)

rem ---- Clean old output ----
if exist build rmdir /s /q build
if exist "dist\%NAME%.exe" del /q "dist\%NAME%.exe"
if exist "dist\%NAME%.exe" (
    echo dist\%NAME%.exe is in use - close the app and run this again.
    goto :fail
)

rem ---- Build: one file, no console window, asks for admin on launch ----
echo.
echo Building %NAME%.exe ...
%PY% -m PyInstaller --noconfirm --clean --onefile --windowed --uac-admin ^
    --name "%NAME%" --icon logo.ico %DATA% ^
    --hidden-import mount_worker --hidden-import fatx --hidden-import mfusepy ^
    app.py
if errorlevel 1 goto :fail
if not exist "dist\%NAME%.exe" goto :fail

echo.
echo ============================================
echo  Done:  %CD%\dist\%NAME%.exe
echo ============================================
pause
exit /b 0

:fail
echo.
echo Build FAILED - see the messages above.
pause
exit /b 1