@echo off
rem Run SyncCam from source on Windows: creates .venv and installs the requirements.
cd /d "%~dp0"
where py >nul 2>nul && (set PY=py -3) || (set PY=python)
%PY% -m venv .venv || goto :fail
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements.txt || goto :fail
echo.
echo Installed. Start SyncCam with start_windows.bat
pause
exit /b 0
:fail
echo Installation failed. Is Python 3.10 or newer installed (python.org)?
pause
exit /b 1
