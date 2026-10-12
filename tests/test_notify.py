import io
import json
import urllib.error
from unittest import mock

import pytest

import notify

PAYLOAD = {"topic": "topic", "title": "Thomas's meals · Mon Oct 12", "message": "🧊 THAW Snack: Soup",
           "priority": 3, "tags": ["clipboard"]}


def http_error(code):
    return urllib.error.HTTPError("https://ntfy.test", code, "err", {}, io.BytesIO())


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return b"{}"


def test_publishes_json_with_unicode_intact():
    with mock.patch("urllib.request.urlopen", return_value=FakeResponse()) as urlopen:
        notify.send_ntfy("https://ntfy.test/", PAYLOAD, token="tk_123")
    req = urlopen.call_args.args[0]
    assert req.full_url == "https://ntfy.test"
    assert req.get_header("Content-type") == "application/json"
    assert req.get_header("Authorization") == "Bearer tk_123"
    assert json.loads(req.data.decode("utf-8")) == PAYLOAD


def test_no_token_no_auth_header():
    with mock.patch("urllib.request.urlopen", return_value=FakeResponse()) as urlopen:
        notify.send_ntfy("https://ntfy.test", PAYLOAD)
    assert urlopen.call_args.args[0].get_header("Authorization") is None


def test_retries_rate_limit_then_succeeds():
    waits = []
    with mock.patch("urllib.request.urlopen", side_effect=[http_error(429), http_error(429), FakeResponse()]) as urlopen:
        notify.send_ntfy("https://ntfy.test", PAYLOAD, sleep=waits.append)
    assert urlopen.call_count == 3 and waits == [4, 8]


def test_gives_up_after_retries():
    waits = []
    with mock.patch("urllib.request.urlopen", side_effect=[http_error(429)] * 4):
        with pytest.raises(urllib.error.HTTPError):
            notify.send_ntfy("https://ntfy.test", PAYLOAD, sleep=waits.append)
    assert waits == [4, 8, 12]


def test_other_errors_are_not_retried():
    with mock.patch("urllib.request.urlopen", side_effect=[http_error(403)]) as urlopen:
        with pytest.raises(urllib.error.HTTPError):
            notify.send_ntfy("https://ntfy.test", PAYLOAD, sleep=lambda s: None)
    assert urlopen.call_count == 1
