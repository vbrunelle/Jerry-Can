# WSL Python Project Setup

A Windows batch script that automates the setup of Python development environments in WSL (Windows Subsystem for Linux) with VS Code integration.

## What It Does

This script automates the entire setup process for Python projects running in WSL from Windows:

1. **Creates a Python virtual environment** in WSL at `~/.venvs/<project_name>`
2. **Installs dependencies** from `requirements-test.txt` and/or `setup.py` if present
3. **Configures VS Code settings** with:
   - Custom WSL terminal profiles with auto-activation
   - Python interpreter path pointing to the WSL virtual environment
   - Pytest configuration
4. **Adds auto-activation to .bashrc** so the virtual environment activates automatically when you navigate to the project directory in WSL

## Prerequisites

- Windows 10/11 with WSL installed
- Ubuntu distribution in WSL (or modify the script for your distribution)
- Python 3 installed in WSL (`sudo apt install python3 python3-venv`)
- VS Code with WSL extension

## How to Run

1. Open a **Windows Command Prompt** or **PowerShell** in your project directory
2. Run the setup script:
   ```cmd
   .\setup.bat
   ```
3. Wait for the setup to complete (it will show progress for each step)
4. **Reload VS Code window**: Press `Ctrl+Shift+P`, type "Reload Window", and press Enter
5. Open a new terminal in VS Code - it should automatically use WSL and activate your virtual environment

## Files

- **setup.bat** - Main setup script (run from Windows)
- **add_bashrc_activation.sh** - Helper script that configures .bashrc for auto-activation (called by setup.bat)

## What Gets Created

- Virtual environment: `~/.venvs/<project_name>/` in WSL
- VS Code settings: `.vscode/settings.json` in your project
- Modified: `~/.bashrc` in WSL (adds auto-activation section)

## Terminal Profiles

After setup, VS Code will have two WSL terminal profiles:

- **Ubuntu (project_name)** - Opens in your project directory with virtual environment activated (default)
- **Ubuntu** - Opens in WSL without auto-activation

## Troubleshooting

- **Script fails**: Make sure WSL is running and Python 3 is installed
- **Virtual environment not activating**: Reload VS Code window after setup
- **Permission denied**: The script may need execute permissions for the .sh file:
  ```bash
  wsl chmod +x add_bashrc_activation.sh
  ```

## Customization

To customize the script for your needs:

- Change the WSL distribution name (default: Ubuntu) in setup.bat
- Modify the virtual environment location (default: `~/.venvs/`)
- Add additional VS Code settings in the JSON generation section
- Adjust pip install commands for different dependency files

## Notes

- The script automatically detects your WSL username
- Virtual environments are stored in `~/.venvs/` to keep them separate from the project directory
- The setup is non-destructive - it won't overwrite existing virtual environments
- The .bashrc modification is idempotent (safe to run multiple times)
