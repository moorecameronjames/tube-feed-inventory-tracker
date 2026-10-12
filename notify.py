"""Push the morning report to phones via ntfy (https://ntfy.sh)."""

import time
import urllib.error
import urllib.request

# ntfy.sh rate-limits by IP unless you send as a logged-in user, and Render's
# free tier shares outbound IPs with other apps, so back off and retry on 429.
RETRY_DELAYS = (4, 8, 12)


def send_ntfy(server, topic, title, message, priority=3, tags=(), click=None, token=None,
              sleep=time.sleep):
    headers = {"Title": title, "Priority": str(priority)}
    if tags:
        headers["Tags"] = ",".join(tags)
    if click:
        headers["Click"] = click
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = f"{server.rstrip('/')}/{topic}"
    for delay in (*RETRY_DELAYS, None):
        req = urllib.request.Request(url, data=message.encode("utf-8"), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                resp.read()
            return
        except urllib.error.HTTPError as e:
            if e.code != 429 or delay is None:
                raise
            sleep(delay)
