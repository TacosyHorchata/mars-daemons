"""Standalone host for the reusable mars_runtime core."""

from .app import create_app

__all__ = ["create_app"]
