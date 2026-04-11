@echo off
setlocal enabledelayedexpansion

echo ============================================
echo WSL Python Project Setup
echo ============================================
echo.

REM Get the current directory name (project name)
for %%I in (.) do set PROJECT_NAME=%%~nxI
echo Project Name: %PROJECT_NAME%
echo Current Directory: %CD%
echo.

REM Convert Windows path to WSL path
set "WSL_PATH=%CD:\=/%"
set "WSL_PATH=/mnt/c%WSL_PATH:~2%"
echo WSL Path: %WSL_PATH%
echo.

REM Get WSL username automatically
echo Detecting WSL username...
for /f "delims=" %%i in ('wsl whoami') do set WSL_USERNAME=%%i
echo WSL Username: %WSL_USERNAME%
echo.

echo Step 1: Creating Python virtual environment in WSL...
wsl bash -c "mkdir -p ~/.venvs && cd '%WSL_PATH%' && python3 -m venv ~/.venvs/%PROJECT_NAME% && echo 'Virtual environment created at ~/.venvs/%PROJECT_NAME%'"
if %ERRORLEVEL% NEQ 0 (
    echo ERROR: Failed to create virtual environment
    pause
    exit /b 1
)
echo.

echo Step 2: Upgrading pip and installing dependencies...
wsl bash -c "source ~/.venvs/%PROJECT_NAME%/bin/activate && pip install --upgrade pip && cd '%WSL_PATH%' && if [ -f 'requirements-test.txt' ]; then pip install -r requirements-test.txt; echo 'Installed test dependencies'; fi && if [ -f 'setup.py' ]; then pip install -e .; echo 'Installed package in development mode'; fi"
if %ERRORLEVEL% NEQ 0 (
    echo WARNING: Some dependencies may not have installed correctly
)
echo.

echo Step 3: Creating VS Code settings...
if not exist .vscode mkdir .vscode

(
echo {
echo     "terminal.integrated.profiles.windows": {
echo         "Ubuntu (%PROJECT_NAME%)": {
echo             "path": "C:\\Windows\\System32\\wsl.exe",
echo             "args": ["-d", "Ubuntu", "--cd", "%WSL_PATH%", "--", "bash", "-c", "source ~/.venvs/%PROJECT_NAME%/bin/activate && exec bash"],
echo             "icon": "terminal-ubuntu"
echo         },
echo         "Ubuntu": {
echo             "path": "C:\\Windows\\System32\\wsl.exe",
echo             "args": ["-d", "Ubuntu"],
echo             "icon": "terminal-ubuntu"
echo         }
echo     },
echo     "terminal.integrated.defaultProfile.windows": "Ubuntu (%PROJECT_NAME%)",
echo     "python.defaultInterpreterPath": "/home/%WSL_USERNAME%/.venvs/%PROJECT_NAME%/bin/python",
echo     "python.terminal.activateEnvironment": true,
echo     "python.testing.pytestEnabled": true,
echo     "python.testing.unittestEnabled": false,
echo     "python.testing.pytestArgs": [
echo         "tests"
echo     ]
echo }
) > .vscode\settings.json

echo VS Code settings created at .vscode\settings.json
echo.

echo Step 4: Adding auto-activation to .bashrc...
wsl bash '%WSL_PATH%/add_bashrc_activation.sh' '%PROJECT_NAME%' '%WSL_PATH%'
echo.

echo Step 5: Verifying installation...
wsl bash -c "source ~/.venvs/%PROJECT_NAME%/bin/activate && python --version && pip list | head -10"
echo.

echo ============================================
echo Setup Complete!
echo ============================================
echo.
echo Project: %PROJECT_NAME%
echo Virtual Environment: ~/.venvs/%PROJECT_NAME%
echo VS Code Settings: .vscode\settings.json
echo.
echo Next Steps:
echo 1. Reload VS Code window (Ctrl+Shift+P, then "Reload Window")
echo 2. Open a new terminal - it should auto-activate the virtual environment
echo 3. Run: pytest
echo.
pause
