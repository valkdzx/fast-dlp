import sys

from . import pathfix
from .cli import main

# `python -m fast_dlp` is what works when `fast-dlp` is not found: fix that for next time.
try:
    pathfix.ensure()
except Exception:                            # never let this stop a download
    pass

sys.exit(main())
