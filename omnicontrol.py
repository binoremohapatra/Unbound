"""omnicontrol.py (Binore): Top-level executable entry point for OmniControl.

Enables running as:
  python omnicontrol.py [args]
or via console_scripts entry point:
  omnicontrol [args]
"""
import sys
from main import main

if __name__ == "__main__":
    sys.exit(main())

