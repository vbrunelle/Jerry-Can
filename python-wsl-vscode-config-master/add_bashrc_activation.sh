#!/bin/bash
# Script to add auto-activation to .bashrc
# Usage: add_bashrc_activation.sh <project_name> <wsl_path>

PROJECT_NAME="$1"
WSL_PATH="$2"

if ! grep -q "$PROJECT_NAME venv auto-activate" ~/.bashrc; then
    cat >> ~/.bashrc << EOFBASHRC

# $PROJECT_NAME venv auto-activate
if [ -d "\$HOME/.venvs/$PROJECT_NAME" ]; then
  case "\$PWD" in
    $WSL_PATH|$WSL_PATH/*)
      source "\$HOME/.venvs/$PROJECT_NAME/bin/activate"
      ;;
  esac
fi
EOFBASHRC
    echo "Auto-activation added to .bashrc"
else
    echo "Auto-activation already exists in .bashrc"
fi
