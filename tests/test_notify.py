import io
import urllib.error
from unittest import mock

import pytest

import notify


def http_error(code):
    return urllib.error.HTTPError("https://ntfy.test/t", code, "err", {}, io.BytesIO())


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return b"{}"


def test_sends_token_and_headers():
    with mock.patch("urllib.request.urlopen", return_value=FakeResponse()) as urlopen:
        notify.send_ntfy("https://ntfy.test/", "topic", "Title", "body", priority=4,
                         tags=["a", "b"], click="https://app.test", token="tk_123")
    req = urlopen.call_args.args[0]
    assert req.full_url == "https://ntfy.test/topic"
    assert req.get_header("Authorization") == "Bearer tk_123"
    assert req.get_header("Tags") == "a,b" and req.get_header("Priority") == "4"
    assert req.data == b"body"


def test_no_token_no_auth_header():
    with mock.patch("urllib.request.urlopen", return_value=FakeResponse()) as urlopen:
        notify.send_ntfy("https://ntfy.test", "topic", "Title", "body")
    assert urlopen.call_args.args[0].get_header("Authorization") is None


def test_retries_rate_limit_then_succeeds():
    waits = []
    with mock.patch("urllib.request.urlopen", side_effect=[http_error(429), http_error(429), FakeResponse()]) as urlopen:
        notify.send_ntfy("https://ntfy.test", "topic", "T", "b", sleep=waits.append)
    assert urlopen.call_count == 3 and waits == [4, 8]


def test_gives_up_after_retries():
    waits = []
    with mock.patch("urllib.request.urlopen", side_effect=[http_error(429)] * 4):
        with pytest.raises(urllib.error.HTTPError):
            notify.send_ntfy("https://ntfy.test", "topic", "T", "b", sleep=waits.append)
    assert waits == [4, 8, 12]


def test_other_errors_are_not_retried():
    with mock.patch("urllib.request.urlopen", side_effect=[http_error(403)]) as urlopen:
        with pytest.raises(urllib.error.HTTPError):
            notify.send_ntfy("https://ntfy.test", "topic", "T", "b", sleep=lambda s: None)
    assert urlopen.call_count == 1
