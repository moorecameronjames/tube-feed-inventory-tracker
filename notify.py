"""Push the morning report to phones via ntfy (https://ntfy.sh)."""

import urllib.request


def send_ntfy(server, topic, title, message, priority=3, tags=(), click=None):
    headers = {"Title": title, "Priority": str(priority)}
    if tags:
        headers["Tags"] = ",".join(tags)
    if click:
        headers["Click"] = click
    req = urllib.request.Request(
        f"{server.rstrip('/')}/{topic}",
        data=message.encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        resp.read()
