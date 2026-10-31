"""Additive, offline-first AI-native portfolio experiment.

This module does not import a sealed Observer/Alpha runtime and has no exchange
trading, account, scheduling, or network collection capability.
"""

from .engine import Experiment, Protocol, ReplayError

__all__ = ["Experiment", "Protocol", "ReplayError"]
