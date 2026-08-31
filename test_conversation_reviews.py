import asyncio
import json
import os
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ.setdefault("SUPABASE_URL", "http://localhost/rest/v1")
os.environ.setdefault("SUPABASE_KEY", "test-supabase-key")
os.environ.setdefault("WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("DASHBOARD_SECRET", "test-dashboard-secret")

import main


class _FakeResponse:
    def __init__(self, data, status_code=200):
        self._data = data
        self.status_code = status_code
        self.text = json.dumps(data)

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}: {self.text}")


class _FakeAsyncClient:
    posts = []
    patches = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, **kwargs):
        if url.endswith("/conversations"):
            return _FakeResponse([
                {
                    "id": "11111111-1111-1111-1111-111111111111",
                    "created_at": "2026-07-03T08:00:00+00:00",
                    "last_inbound_at": "2026-07-03T08:03:00+00:00",
                    "username": "prospect_1",
                    "display_name": "prospect_1",
                    "status": "en_cours",
                    "automation_mode": "auto",
                    "history": [
                        {"role": "user", "content": "What is Angellos?", "timestamp": "2026-07-03T08:00:00+00:00"},
                        {"role": "assistant", "content": "Angellos helps with DMs. Want a call?", "timestamp": "2026-07-03T08:01:00+00:00", "sent": True},
                        {"role": "user", "content": "That feels a bit fast", "timestamp": "2026-07-03T08:03:00+00:00"},
                    ],
                }
            ])
        if url.endswith("/prompt_versions"):
            params = kwargs.get("params") or {}
            if params.get("source") == "eq.postmortem":
                return _FakeResponse([])
            return _FakeResponse([{"id": "active-version-1", "content": "Base Angellos prompt", "is_active": True, "source": "manual"}])
        if url.endswith("/conversation_reviews"):
            return _FakeResponse([])
        return _FakeResponse([])

    async def post(self, url, **kwargs):
        self.__class__.posts.append({"url": url, **kwargs})
        row = dict(kwargs["json"])
        if url.endswith("/prompt_versions"):
            row.update({"id": "prompt-version-1", "created_at": "2026-07-10T09:00:00+00:00"})
        else:
            row.update({"id": "review-1", "created_at": "2026-07-03T09:00:00+00:00"})
        return _FakeResponse([row])

    async def patch(self, url, **kwargs):
        self.__class__.patches.append({"url": url, **kwargs})
        row = dict(kwargs.get("json") or {})
        row.update({
            "id": "review-1",
            "review_date": "2026-07-03",
            "conversation_id": "11111111-1111-1111-1111-111111111111",
            "username": "prospect_1",
        })
        return _FakeResponse([row])


class _FakeMessages:
    def create(self, **kwargs):
        if kwargs.get("max_tokens") == 8192:
            synthesis = {
                "prompt_proposed": "Base Angellos prompt\n- Do not suggest a call before answering the prospect's first context question.",
                "diff": [
                    {"line": "- Do not suggest a call before answering the prospect's first context question.", "type": "add", "justification": "Repeated premature pitch failure."}
                ],
                "summary": "Le post-mortem détecte un pitch trop rapide; proposition de renforcer la règle avant appel.",
            }
            return SimpleNamespace(content=[SimpleNamespace(text=json.dumps(synthesis))])
        review = {
            "objective_reached": False,
            "objective_reason": "The prospect showed friction and Angellos pushed too fast toward a call.",
            "human_likeness_score": 5,
            "sales_effectiveness_score": 4,
            "engagement_score": 6,
            "moment_of_failure": "assistant reply after first question",
            "failure_category": "too_commercial",
            "what_angellos_did_wrong": "It moved to a call before acknowledging the question and building trust.",
            "better_human_reply": "Yeah, it helps handle replies and qualify people before you jump in. How are you managing DMs right now?",
            "lesson_learned": "When a prospect asks what Angellos is, answer briefly then ask about their current DM flow before suggesting a call.",
            "prompt_rule_candidate": "If a prospect asks what Angellos is, answer in one casual sentence and ask about their current DM flow before mentioning a call.",
        }
        return SimpleNamespace(content=[SimpleNamespace(text=json.dumps(review))])


class _FakeClient:
    messages = _FakeMessages()


def test_normalize_conversation_review_clamps_scores_and_fields():
    review = main.normalize_conversation_review(
        {
            "objective_reached": True,
            "human_likeness_score": 99,
            "sales_effectiveness_score": "0",
            "engagement_score": "7",
            "failure_category": "unknown",
        },
        {"id": "conv-1", "username": "prospect"},
    )
    assert review["conversation_id"] == "conv-1"
    assert review["human_likeness_score"] == 10
    assert review["sales_effectiveness_score"] == 1
    assert review["engagement_score"] == 7
    assert review["failure_category"] == "objective_reached"


def test_daily_review_job_reviews_full_seeded_conversation_and_persists(monkeypatch):
    _FakeAsyncClient.posts = []
    monkeypatch.setattr(main, "client", _FakeClient())
    monkeypatch.setattr(main.httpx, "AsyncClient", _FakeAsyncClient)

    result = asyncio.run(main.run_daily_conversation_review_job(
        user_id="00000000-0000-0000-0000-000000000001",
        review_date="2026-07-03",
        limit=5,
    ))

    assert result["success"] is True
    assert result["selected"] == 1
    assert result["stored"] == 1
    assert result["reviews"][0]["objective_reached"] is False
    assert result["reviews"][0]["failure_category"] == "too_commercial"
    assert result["reviews"][0]["lesson_status"] == "candidate"
    persisted = _FakeAsyncClient.posts[-1]["json"]
    assert persisted["conversation_id"] == "11111111-1111-1111-1111-111111111111"
    assert persisted["prompt_rule_candidate"].startswith("If a prospect asks what Angellos is")
    assert persisted["raw_review"]["better_human_reply"]


def test_daily_review_endpoint_requires_dashboard_secret():
    with pytest.raises(HTTPException) as exc:
        asyncio.run(main.run_daily_reviews(main.DailyReviewPayload(user_id="user-1"), x_dashboard_secret=None))
    assert exc.value.status_code == 401


def test_postmortem_review_job_creates_inactive_prompt_candidate(monkeypatch):
    _FakeAsyncClient.posts = []
    monkeypatch.setattr(main, "client", _FakeClient())
    monkeypatch.setattr(main.httpx, "AsyncClient", _FakeAsyncClient)
    async def fake_fetch_conversations(user_id, start, end, limit, conversation_id=None):
        return [{
            "id": "11111111-1111-1111-1111-111111111111",
            "created_at": main.datetime.now(main.timezone.utc).isoformat(),
            "last_inbound_at": main.datetime.now(main.timezone.utc).isoformat(),
            "username": "prospect_1",
            "display_name": "prospect_1",
            "status": "en_cours",
            "automation_mode": "auto",
            "history": [
                {"role": "user", "content": "What is Angellos?", "timestamp": "2026-07-03T08:00:00+00:00"},
                {"role": "assistant", "content": "Angellos helps with DMs. Want a call?", "timestamp": "2026-07-03T08:01:00+00:00", "sent": True},
                {"role": "user", "content": "That feels a bit fast", "timestamp": "2026-07-03T08:03:00+00:00"},
            ],
        }]
    monkeypatch.setattr(main, "fetch_conversations_for_review", fake_fetch_conversations)

    result = asyncio.run(main.run_weekly_conversation_postmortem_job(
        user_id="00000000-0000-0000-0000-000000000001",
        days=7,
        limit=5,
    ))

    assert result["success"] is True
    assert result["selected"] == 1
    assert result["reviewed"] == 1
    assert result["prompt_version_id"] == "prompt-version-1"
    assert result["prompt_changed"] is True
    assert result["is_active"] is False
    assert result["source"] == "postmortem"
    assert result["top_failures"] == [{"failure_category": "too_commercial", "count": 1}]

    prompt_insert = [post for post in _FakeAsyncClient.posts if post["url"].endswith("/prompt_versions")][-1]["json"]
    assert prompt_insert["is_active"] is False
    assert prompt_insert["source"] == "postmortem"
    assert prompt_insert["previous_version_id"] == "active-version-1"
    assert prompt_insert["prompt_diff"][0]["type"] == "add"
    assert "pitch trop rapide" in prompt_insert["refinement_instruction"]
