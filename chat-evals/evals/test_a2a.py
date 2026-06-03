#!/usr/bin/env python3
"""Quick test to inspect raw A2A endpoint response."""

import json
import os
import sys
import uuid

import requests
from dotenv import load_dotenv

# Walk up to find .env
_dir = os.path.dirname(os.path.abspath(__file__))
while _dir != os.path.dirname(_dir):
    env_path = os.path.join(_dir, ".env")
    if os.path.isfile(env_path):
        load_dotenv(env_path)
        break
    _dir = os.path.dirname(_dir)

base_url = os.environ.get("A2A_BASE_URL", "")
func_key = os.environ.get("A2A_FUNC_KEY", "")
if base_url and func_key:
    sep = "&" if "?" in base_url else "?"
    url = f"{base_url}{sep}code={func_key}"
else:
    url = base_url

if not url:
    print("ERROR: A2A_BASE_URL not set in .env")
    sys.exit(1)

question = sys.argv[1] if len(sys.argv) > 1 else "Analyse my profile"

payload = {
    "jsonrpc": "2.0",
    "id": str(uuid.uuid4()),
    "method": "message/send",
    "params": {
        "message": {
            "kind": "message",
            "messageId": str(uuid.uuid4()),
            "role": "user",
            "parts": [{"kind": "text", "text": question}],
        },
    },
}

print(f"URL: {url}")
print(f"Question: {question}")
print("Sending request...\n")

resp = requests.post(url, json=payload, timeout=120)
print(f"Status: {resp.status_code}\n")
print(json.dumps(resp.json(), indent=2))
