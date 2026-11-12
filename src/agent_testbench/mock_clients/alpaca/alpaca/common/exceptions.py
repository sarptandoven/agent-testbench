"""alpaca.common.exceptions of the mock alpaca package: the SDK's APIError, read the same way."""
import json

__all__ = ["APIError", "RetryException"]


class RetryException(Exception):
    pass


class APIError(Exception):
    """An error the API answered: str(e) is its JSON body; .code, .message, .status_code, .request and .response
    read as in alpaca-py."""

    def __init__(self, error, http_error=None):
        super().__init__(error)
        self._error = error
        self._http_error = http_error

    @property
    def code(self):
        return json.loads(self._error)["code"]

    @property
    def message(self):
        return json.loads(self._error)["message"]

    @property
    def status_code(self):
        http_error = self._http_error
        if http_error is not None and hasattr(http_error, "response"):
            return http_error.response.status_code

    @property
    def request(self):
        if self._http_error is not None:
            return self._http_error.request

    @property
    def response(self):
        if self._http_error is not None:
            return self._http_error.response
