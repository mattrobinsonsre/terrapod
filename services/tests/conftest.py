"""
Top-level test configuration for Terrapod.
"""

import os

# Ensure test-friendly defaults
os.environ.setdefault("TERRAPOD_STORAGE__BACKEND", "filesystem")
os.environ.setdefault("TERRAPOD_JSON_LOGS", "false")
os.environ.setdefault("TERRAPOD_LOG_LEVEL", "DEBUG")
