#!/usr/bin/env python3
"""WSGI entry point for PythonAnywhere and any other WSGI host.

On PythonAnywhere the web app's own WSGI file (not this file) sets the
environment variables and imports `application` from main.py. See
pythonanywhere_wsgi_snippet.py for the exact file to paste there.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from main import application  # noqa: E402,F401  (WSGI callable)
