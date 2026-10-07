"""Secrets come from environment variables of the same name (a Modal secret, a CI secret, a local export).
Values are never printed."""
import os


class SecretNotFoundError(Exception):
    pass


class NotebookAccessError(Exception):
    pass


class TimeoutException(Exception):
    pass


def get(key):
    value = os.environ.get(key)
    if value is None:
        raise SecretNotFoundError(f"Secret {key} does not exist. Headless runs read it from the environment: "
                                  f"list it under the step's `secrets:` (Modal) or export it before the run.")
    return value
