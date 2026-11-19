"""Cancellation shared by readers and the application without import cycles."""


class AnalysisStopped(Exception):
    """A user stop request; never classify it as corrupt data or retry it."""
