"""Public account errors without SQL or token values."""


class AccountStorageError(Exception):
    """Account storage is unavailable or could not complete the operation."""
