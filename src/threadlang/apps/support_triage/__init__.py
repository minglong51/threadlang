"""Support-triage — the vertical-slice app (L6).

A concrete multi-agent product on the ThreadLang stack: triage a support ticket
(classify priority + find KB articles via the app's own tools), then draft a
customer reply — run durably, submittable over the control-plane API, and
inspectable on the dashboard. See `app.py` for the entrypoint.
"""

from .app import load_program, main
from .tools import build_registry

__all__ = ["build_registry", "load_program", "main"]
