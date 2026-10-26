"""The PayPal balance read through the APIMatic SDK, against a stub transport (no network).
The stub sits under the real SDK client, so request building and decoding run for real."""
import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from decimal import Decimal

import httpx2
import pytest
from paypal import PaypalClient
from paypal.core import ClientCredentials, HttpRequest, HttpResponse

from app.config import Settings
from app.paypal_balances import BalanceReader
from app.paypal_client import PayPalError


@dataclass
class StubResponse:
    status_code: int = 200
    headers: Mapping[str, str] = field(default_factory=dict)
    chunks: list[bytes] = field(default_factory=list)
    url: str = "https://api-m.sandbox.paypal.com/"
    closed: bool = False

    def iter_bytes(self, chunk_size: int | None) -> Iterator[bytes]:
        yield from self.chunks
        self.close()

    def read(self) -> bytes:
        self.close()
        return b"".join(self.chunks)

    def close(self) -> None:
        self.closed = True


class StubTransport:
    """send() + close(): the SDK's sync transport protocol. Records every request, token fetch included."""

    def __init__(self, *responses: StubResponse, error: Exception | None = None) -> None:
        self._responses = list(responses)
        self._error = error
        self.requests: list[HttpRequest] = []

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        if not self._responses and self._error is not None:
            raise self._error
        return self._responses.pop(0)

    def close(self) -> None: ...


def json_response(status: int, body: object) -> StubResponse:
    return StubResponse(status_code=status, headers={"content-type": "application/json"},
                        chunks=[json.dumps(body).encode()])


def token() -> StubResponse:
    return json_response(200, {"access_token": "t", "token_type": "Bearer", "expires_in": 3600})


def reader(transport: StubTransport) -> BalanceReader:
    return BalanceReader(PaypalClient(
        base_url="https://api-m.sandbox.paypal.com", oauth2=ClientCredentials(client_id="id", client_secret="secret"),
        custom_http_client=transport, retry_options=0))


def balances(*entries: dict) -> StubResponse:
    return json_response(200, {"balances": list(entries), "account_id": "ACC"})


def usd(available: str | None, total: str = "0.00") -> dict:
    entry = {"currency": "USD", "total_balance": {"currency_code": "USD", "value": total}}
    if available is not None:
        entry["available_balance"] = {"currency_code": "USD", "value": available}
    return entry


def test_reads_the_available_balance_and_asks_for_the_currency():
    transport = StubTransport(token(), balances(usd("-12398.81", "-12398.81")))
    assert reader(transport).available("USD") == Decimal("-12398.81")
    req = transport.requests[-1]  # the operation, not the token fetch
    assert req.method == "GET" and "/v1/reporting/balances" in req.url and "currency_code=USD" in req.url
    assert req.headers["authorization"] == "Bearer t"


def test_falls_back_to_the_total_when_paypal_gives_no_available_figure():
    assert reader(StubTransport(token(), balances(usd(None, "250.00")))).available("USD") == Decimal("250.00")


@pytest.mark.parametrize("body", [{}, {"balances": [{"currency": "EUR", "total_balance":
                                                     {"currency_code": "EUR", "value": "5.00"}}]}])
def test_no_balance_in_the_currency_is_none_not_zero(body):
    assert reader(StubTransport(token(), json_response(200, body))).available("USD") is None


def test_paypal_rejection_keeps_its_status_and_reason():
    error = {"name": "NOT_AUTHORIZED", "message": "Authorization failed.", "debug_id": "dbg-9"}
    with pytest.raises(PayPalError) as e:
        reader(StubTransport(token(), json_response(403, error))).available("USD")
    assert (e.value.status, e.value.name, e.value.debug_id) == (403, "NOT_AUTHORIZED", "dbg-9")


def test_an_undocumented_status_is_still_a_paypal_error():
    with pytest.raises(PayPalError) as e:
        reader(StubTransport(token(), StubResponse(status_code=418, chunks=[b"teapot"]))).available("USD")
    assert e.value.status == 418 and "teapot" in e.value.message


def test_refused_credentials_are_reported_as_ours_not_the_callers():
    transport = StubTransport(json_response(401, {"error": "invalid_client", "error_description": "bad secret"}))
    with pytest.raises(PayPalError) as e:
        reader(transport).available("USD")
    assert (e.value.status, e.value.name) == (502, "AUTH_FAILED")
    assert len(transport.requests) == 1  # only the token fetch: the balance was never asked for


def test_an_unreadable_body_is_not_reported_as_a_balance():
    with pytest.raises(PayPalError) as e:
        reader(StubTransport(token(), json_response(200, {"balances": "not-a-list"}))).available("USD")
    assert e.value.name == "UNREADABLE_RESPONSE"


@pytest.mark.parametrize("error", [httpx2.ConnectError("refused"), httpx2.ReadTimeout("no reply")])
def test_network_failures_mean_the_balance_is_unknown(error):
    # A read has no side effect, so never-sent and maybe-landed lead to the same outcome here.
    transport = StubTransport(token(), error=error)
    with pytest.raises(PayPalError) as e:
        reader(transport).available("USD")
    assert (e.value.status, e.value.name) == (504, "UNREACHABLE")
    assert "/v1/reporting/balances" in transport.requests[-1].url


def test_from_settings_uses_the_configured_paypal_host():
    transport = StubTransport(token(), balances(usd("10.00")))
    settings = Settings(paypal_api_base="https://api-m.example.test", paypal_client_id="id",
                        paypal_client_secret="secret")
    with BalanceReader.from_settings(settings, transport=transport) as r:
        assert r.available("USD") == Decimal("10.00")
    assert all(req.url.startswith("https://api-m.example.test/") for req in transport.requests)
