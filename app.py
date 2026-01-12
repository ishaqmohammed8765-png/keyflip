from __future__ import annotations

import os
import subprocess
import sys
import streamlit as st
from pathlib import Path

# Set browser path environment variable to a writable local folder
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(Path(".playwright").resolve())

# --- Ensure Playwright Chromium is installed before anything else ---
if not st.session_state.get('playwright_installed', False):
    with st.spinner('Installing Chromium for Playwright…'):
        try:
            result = subprocess.run(
                [sys.executable, "-m", "playwright", "install", "chromium"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            if result.returncode != 0:
                # Try full install as fallback
                st.warning("Chromium install failed. Trying full browser install as fallback…")
                fallback = subprocess.run(
                    [sys.executable, "-m", "playwright", "install"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                if fallback.returncode != 0:
                    raise RuntimeError(fallback.stderr or fallback.stdout)
            st.success("✅ Chromium (Playwright) installed successfully.")
            print(result.stdout or fallback.stdout)
        except Exception as e:
            st.error("❌ Chromium installation failed completely.\n\nPlease check your internet connection or try restarting the app.")
            st.code(str(e))
            st.stop()
    st.session_state.playwright_installed = True

# Continue with the rest of your app imports and logic below

import io
import time
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from typing import Optional, Tuple

import pandas as pd

from keyflip.main import main as keyflip_main

# [Your existing Streamlit app logic continues here...]
# Add buttons, run logic, outputs, etc.

st.set_page_config(page_title="Keyflip Scanner", layout="wide")

st.title("Keyflip — Fanatical → Eneba Scanner")
st.caption("Scan Fanatical and compare with Eneba to find profitable deals.")

# Implement the rest of your UI and scan logic below...
# You can re-integrate your settings, scan buttons, results display, etc.
