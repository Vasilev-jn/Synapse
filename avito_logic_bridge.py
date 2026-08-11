"""Backward-compatible shim for already running Monitor processes.

New code should import from app.pipeline directly.
"""

from app.pipeline import *  # noqa: F401,F403
