"""alpaca.data.historical.crypto of the mock alpaca package: CryptoHistoricalDataClient (no key needed, as with the
real one)."""
from ..enums import CryptoFeed
from .stock import _credentials, _DataClient

__all__ = ["CryptoHistoricalDataClient"]


class CryptoHistoricalDataClient(_DataClient):
    _has = "get_crypto_bars"

    def __init__(self, api_key=None, secret_key=None, oauth_token=None, raw_data=False, url_override=None,
                 use_basic_auth=False, sandbox=False):
        _credentials("CryptoHistoricalDataClient", api_key, secret_key, oauth_token, required=False)
        self._raw = raw_data

    def get_crypto_bars(self, request_params, feed=CryptoFeed.US):
        return self._bars("get_crypto_bars", request_params, crypto=True)
