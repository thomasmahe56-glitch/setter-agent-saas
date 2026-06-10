import urllib.request
import json
import os

payload = json.dumps({
    "username": "julien_runs",
    "message": "Salut, j'ai commenté Analyse sur ton post",
    "subscriber_id": "julien_runs_test",
}).encode()

req = urllib.request.Request(
    "http://127.0.0.1:8000/webhook",
    data=payload,
    headers={
        "Content-Type": "application/json",
        "X-Webhook-Secret": os.getenv("WEBHOOK_SECRET", ""),
    },
    method="POST"
)

with urllib.request.urlopen(req) as res:
    data = json.loads(res.read())
    print(json.dumps({
        "agent_response": data.get("agent_response", ""),
        "should_send": data.get("should_send"),
        "mode": data.get("mode"),
        "suggested_response": data.get("suggested_response", ""),
    }, ensure_ascii=False, indent=2))
