#!/bin/bash
cd /Volumes/AI_Workspace/iris_studio
source .venv/bin/activate
# Long-lived setup-token from Keychain when installed (DQ-49); no-op otherwise.
. scripts/claude_token.sh
exec python iris.py
