"""Exceptions raised by the worker package."""


class WorkerError(Exception):
    """Base class for every exception this package raises."""


class WorkerConfigurationError(WorkerError):
    """The worker's environment is missing something it cannot run without.

    Deliberately not caught by runner.main: a misconfigured unit should fail
    loudly with this message in the journal, not be reported as an
    operational failure of a job that never started.
    """
