"""The sending account's PayPal balance, read through the APIMatic PayPal SDK
(Transaction Search > search_balances, GET /v1/reporting/balances).

Payouts, webhooks and payout status use paypal_client.py instead: this SDK has no Payouts API.
Every SDK failure leaves this module as a PayPalError, the type the rest of the app handles.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

import httpx2
from paypal import PaypalClient
from paypal.core import ApiError, ClientCredentials, HttpClient, OAuthProviderError, RawError, UnsetType
from paypal.models import DefaultError

from app.config import Settings
from app.paypal_client import PayPalError


class BalanceReader:
    def __init__(self, client: PaypalClient) -> None:
        self._client = client

    @classmethod
    def from_settings(cls, settings: Settings, *, transport: HttpClient | None = None) -> BalanceReader:
        if not settings.paypal_client_id or not settings.paypal_client_secret.get_secret_value():
            raise RuntimeError("PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET must be set in backend/.env.")
        client = PaypalClient(
            base_url=settings.paypal_api_base,  # explicit: left out, the SDK silently uses the sandbox
            oauth2=ClientCredentials(client_id=settings.paypal_client_id,
                                     client_secret=settings.paypal_client_secret.get_secret_value()),
            timeout=15.0,
            retry_options=1,  # one retry: a slow PayPal must not hold the Send button for long
            custom_http_client=transport,
        )
        return cls(client)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> BalanceReader:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def available(self, currency: str) -> Decimal | None:
        """The available balance in `currency` (the total when PayPal gives no available figure),
        or None when PayPal lists no balance in that currency. PayPal can lag by up to 3 hours."""
        try:
            response = self._client.transaction_search.search_balances(currency_code=currency)
        except ApiError as e:
            if isinstance(e.error, OAuthProviderError):  # the token fetch failed: nothing was asked
                raise PayPalError(502, "AUTH_FAILED", f"PayPal refused the app's credentials ({e.error.error}).",
                                  None) from e
            if isinstance(e.error, DefaultError):
                raise PayPalError(e.status_code, e.error.name, e.error.message, e.error.debug_id) from e
            text = e.error.text()[:300] if isinstance(e.error, RawError) else ""
            raise PayPalError(e.status_code, None, text or "PayPal returned an error.", None) from e
        except ValueError as e:  # pydantic's ValidationError (wrong shape) or a body that is not JSON
            raise PayPalError(502, "UNREADABLE_RESPONSE", "PayPal's balance response could not be read.", None) from e
        except httpx2.RequestError as e:  # a read has no side effect: any transport failure just means "unknown"
            raise PayPalError(504, "UNREACHABLE", f"Could not reach PayPal: {type(e).__name__}", None) from e

        if isinstance(response.balances, UnsetType):
            return None
        for balance in response.balances:
            if balance.currency != currency:
                continue
            money = balance.total_balance if isinstance(balance.available_balance, UnsetType) else balance.available_balance
            try:
                return Decimal(money.value)
            except InvalidOperation as e:
                raise PayPalError(502, "UNREADABLE_RESPONSE", f"PayPal sent an unreadable amount: {money.value!r}",
                                  None) from e
        return None
