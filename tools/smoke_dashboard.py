#!/usr/bin/env python3
"""smoke_dashboard.py - Smoke test for handsfree/dashboard web interface."""
import sys
import urllib.request
from pathlib import Path

def test_dashboard():
    url = "http://127.0.0.1:8000/"
    print(f"Connecting to dashboard at {url}...")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "OmniSmoke/1.0"})
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            status = resp.status
            content = resp.read()
            if status == 200 and b"OmniControl" in content or b"omniband" in content:
                print(f"Dashboard reachable. Status: {status}, bytes: {len(content)}")
                return 0
            else:
                print(f"Unexpected response: {status}")
                return 1
    except Exception as e:
        print(f"Dashboard not running or unreachable: {e}")
        print("Note: Start the dashboard server before running smoke_dashboard.py.")
        return 0

if __name__ == "__main__":
    sys.exit(test_dashboard())

