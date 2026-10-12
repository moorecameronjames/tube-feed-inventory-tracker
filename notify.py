"""Push the morning report to phones via ntfy (https://ntfy.sh)."""

import json
import time
import urllib.error
import urllib.request

# ntfy.sh's free tier rate-limits by sender IP, and Render's free tier shares its
# outbound IPs with other apps, so back off and retry on 429. (The scheduled
# morning report is sent from GitHub Actions instead; see morning-report.yml.)
RETRY_DELAYS = (4, 8, 12)


def send_ntfy(server, payload, token=None, sleep=time.sleep):
    """Publish one message. `payload` is ntfy's JSON format: topic, title, message,
    priority, tags, click. JSON (unlike headers) keeps non-ASCII titles intact."""
    headers = {"Content-Type": "application/json"}
    if token:
        # Only lifts the rate limit on a paid ntfy plan; free accounts are still per-IP.
        headers["Authorization"] = f"Bearer {token}"
    body = json.dumps(payload).encode("utf-8")
    for delay in (*RETRY_DELAYS, None):
        req = urllib.request.Request(server.rstrip("/"), data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                resp.read()
            return
        except urllib.error.HTTPError as e:
            if e.code != 429 or delay is None:
                raise
            sleep(delay)
