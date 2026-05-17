#!/bin/bash
# Double-click this file to start the RoK Scanner web UI.
cd "$(dirname "$0")"
source venv/bin/activate
python server.py
