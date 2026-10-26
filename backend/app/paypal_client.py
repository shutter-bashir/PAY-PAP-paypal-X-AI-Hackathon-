"""Thin client for the PayPal REST endpoints this app uses (plain REST over httpx).

Only this module and config.py ever touch the PayPal client secret.
"""
from __future__ import annotations

import time
from collections.abc import Mapping
from decimal import Decimal

import httpx


class PayPalError(Exception):
    def __init__(self, status: int, name: str | None, message: str, debug_id: str | None, body: object = None):
        super().__init__(f"PayPal {status} {name or ''}: {message}".strip())
        self.status = status
        self.name = name
        self.message = message
        self.debug_id = debug_id
        self.body = body

    @classmethod
    def from_response(cls, response: httpx.Response) -> PayPalError:
        body = safe_json(response)
        if isinstance(body, dict):
            name = body.get("name") or body.get("error")
            message = body.get("message") or body.get("error_description") or response.reason_phrase
        else:
            name, message = None, response.text[:300] or response.reason_phrase
        return cls(response.status_code, name, message, debug_id(response), body)


def safe_json(response: httpx.Response) -> object:
    try:
        return response.json()
    except ValueError:
        return None


def debug_id(response: httpx.Response) -> str | None:
    body = safe_json(response)
    return response.headers.get("paypal-debug-id") or (body.get("debug_id") if isinstance(body, dict) else None)


class PayPalClient:
    def __init__(self, base_url: str, client_id: str, client_secret: str, *,
                 timeout: float = 30.0, transport: httpx.BaseTransport | None = None):
        if not client_id or not client_secret:
            raise RuntimeError("PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET must be set in backend/.env.")
        self._client_id = client_id
        self._client_secret = client_secret
        self._http = httpx.Client(base_url=base_url, timeout=timeout, transport=transport)
        self._token: str | None = None
        self._token_expires_at = 0.0

    @classmethod
    def from_settings(cls, settings, **kwargs) -> PayPalClient:
        return cls(settings.paypal_api_base, settings.paypal_client_id,
                   settings.paypal_client_secret.get_secret_value(), **kwargs)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> PayPalClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- Payouts -----------------------------------------------------------

    def create_payout(self, body: Mapping, *, request_id: str) -> httpx.Response:
        """POST /v1/payments/payouts. Returns the raw response: the caller decides what
        each status means. Deduplication rests on sender_batch_id in the body; the
        PayPal-Request-Id header is sent too, but the sandbox does not replay results for it."""
        return self._request("POST", "/v1/payments/payouts", json=body, headers={"PayPal-Request-Id": request_id})

    def get_payout_batch(self, payout_batch_id: str, *, page: int = 1, page_size: int = 1000) -> dict:
        response = self._request("GET", f"/v1/payments/payouts/{payout_batch_id}",
                                 params={"page": page, "page_size": page_size, "total_required": "true"})
        if response.status_code != 200:
            raise PayPalError.from_response(response)
        return response.json()

    # --- Balance -----------------------------------------------------------

    def available_balance(self, currency: str) -> Decimal | None:
        """The sending account's available balance in `currency` (GET /v1/reporting/balances),
        or None when PayPal lists no balance in that currency."""
        response = self._request("GET", "/v1/reporting/balances", params={"currency_code": currency})
        if response.status_code != 200:
            raise PayPalError.from_response(response)
        for balance in (safe_json(response) or {}).get("balances", []):
            if balance.get("currency") == currency and balance.get("available_balance"):
                return Decimal(str(balance["available_balance"]["value"]))
        return None

    # --- Webhooks ----------------------------------------------------------

    def verify_webhook_signature(self, *, headers: Mapping[str, str], event: Mapping, webhook_id: str) -> bool:
        """Asks PayPal whether a webhook delivery is genuine (POST /v1/notifications/verify-webhook-signature)."""
        lower = {k.lower(): v for k, v in headers.items()}
        required = ("paypal-auth-algo", "paypal-cert-url", "paypal-transmission-id",
                    "paypal-transmission-sig", "paypal-transmission-time")
        if not webhook_id or any(not lower.get(h) for h in required):
            return False
        response = self._request("POST", "/v1/notifications/verify-webhook-signature", json={
            "auth_algo": lower["paypal-auth-algo"],
            "cert_url": lower["paypal-cert-url"],
            "transmission_id": lower["paypal-transmission-id"],
            "transmission_sig": lower["paypal-transmission-sig"],
            "transmission_time": lower["paypal-transmission-time"],
            "webhook_id": webhook_id,
            "webhook_event": event,
        })
        if response.status_code != 200:
            raise PayPalError.from_response(response)
        return response.json().get("verification_status") == "SUCCESS"

    def list_webhooks(self) -> list[dict]:
        response = self._request("GET", "/v1/notifications/webhooks")
        if response.status_code != 200:
            raise PayPalError.from_response(response)
        return response.json().get("webhooks", [])

    def list_webhook_event_types(self) -> list[str]:
        response = self._request("GET", "/v1/notifications/webhooks-event-types")
        if response.status_code != 200:
            raise PayPalError.from_response(response)
        return [e["name"] for e in response.json().get("event_types", [])]

    def create_webhook(self, url: str, event_types: list[str]) -> dict:
        response = self._request("POST", "/v1/notifications/webhooks",
                                 json={"url": url, "event_types": [{"name": n} for n in event_types]})
        if response.status_code not in (200, 201):
            raise PayPalError.from_response(response)
        return response.json()

    def update_webhook(self, webhook_id: str, url: str, event_types: list[str]) -> dict:
        response = self._request("PATCH", f"/v1/notifications/webhooks/{webhook_id}", json=[
            {"op": "replace", "path": "/url", "value": url},
            {"op": "replace", "path": "/event_types", "value": [{"name": n} for n in event_types]},
        ])
        if response.status_code != 200:
            raise PayPalError.from_response(response)
        return response.json()

    # --- Plumbing ----------------------------------------------------------

    def _access_token(self, *, force: bool = False) -> str:
        if not force and self._token and time.monotonic() < self._token_expires_at:
            return self._token
        response = self._http.post("/v1/oauth2/token", data={"grant_type": "client_credentials"},
                                   auth=(self._client_id, self._client_secret), headers={"Accept": "application/json"})
        if response.status_code != 200:
            raise PayPalError.from_response(response)
        body = response.json()
        self._token = body["access_token"]
        self._token_expires_at = time.monotonic() + max(int(body.get("expires_in", 300)) - 60, 30)
        return self._token

    def _request(self, method: str, path: str, *, json: object = None, params: Mapping | None = None,
                 headers: Mapping[str, str] | None = None) -> httpx.Response:
        def send(token: str) -> httpx.Response:
            return self._http.request(method, path, json=json, params=params, headers={
                "Authorization": f"Bearer {token}", "Content-Type": "application/json",
                "Accept": "application/json", **(headers or {})})

        response = send(self._access_token())
        if response.status_code == 401:
            # Token revoked or expired early. A 401 means nothing was processed, so resending is safe.
            response = send(self._access_token(force=True))
        return response
