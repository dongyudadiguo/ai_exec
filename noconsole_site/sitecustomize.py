"""Hide console windows for subprocesses started from pythonw tool children.

Loaded automatically because viewer.py puts this directory on PYTHONPATH.
"""

from __future__ import annotations

import os
import subprocess
import sys

if os.name == "nt" and not getattr(subprocess, "_ae_noconsole_patched", False):
    _CREATE_NO_WINDOW = 0x08000000
    _orig_Popen = subprocess.Popen

    class Popen(_orig_Popen):
        def __init__(self, *args, **kwargs):
            # Only force-hide when the current interpreter has no console of its own.
            # This keeps normal interactive console python usable.
            try:
                import ctypes
                has_console = bool(ctypes.windll.kernel32.GetConsoleWindow())
            except Exception:
                has_console = True

            if not has_console:
                kwargs["creationflags"] = int(kwargs.get("creationflags", 0)) | _CREATE_NO_WINDOW
                startupinfo = kwargs.get("startupinfo")
                if startupinfo is None:
                    startupinfo = subprocess.STARTUPINFO()
                    kwargs["startupinfo"] = startupinfo
                try:
                    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                    startupinfo.wShowWindow = 0
                except Exception:
                    pass

            super().__init__(*args, **kwargs)

    subprocess.Popen = Popen
    subprocess._ae_noconsole_patched = True

    # subprocess.run / call / check_* all go through Popen, so this is enough.
