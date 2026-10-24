@echo off
rem Builds dist\always-learn.exe, a single file that runs without Python.
cd /d "%~dp0"

if not exist .venv\Scripts\python.exe (
    py -m venv .venv || python -m venv .venv || goto :error
)
.venv\Scripts\python.exe -m pip install -r requirements.txt pyinstaller || goto :error

rem Generate the UI Automation bindings so they get bundled into the exe.
.venv\Scripts\python.exe -c "import comtypes.client; comtypes.client.GetModule('UIAutomationCore.dll')" || goto :error

.venv\Scripts\pyinstaller --noconfirm --onefile --windowed ^
    --name always-learn ^
    --icon assets\icon.ico ^
    --add-data "assets;assets" ^
    --collect-submodules comtypes.gen ^
    always-learn.py || goto :error

echo.
echo Done: dist\always-learn.exe
exit /b 0

:error
echo.
echo Build failed.
exit /b 1
