#!/bin/bash
# HYDROGUARD System Launcher

# Get the directory where the script is located
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
cd "$SCRIPT_DIR"

# Check if the virtual environment exists
if [ -d ".venv" ]; then
    echo "Activating Virtual Environment..."
    source .venv/bin/activate
else
    echo "Warning: Virtual environment '.venv' not found."
    echo "Attempting to run with system Python..."
fi

# Run the GUI Launcher
echo "Starting HYDROGUARD Controller..."
python3 launcher.py
