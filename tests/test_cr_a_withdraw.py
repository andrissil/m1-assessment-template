"""CR-A: iedzīvotājs atsauc iesniegumu (POST /submissions/{id}/withdraw)."""

import logging
from datetime import datetime, timezone

import pytest

from app import clock, storage

REASON = "Problēma jau ir atrisināta"


@pytest.fixture
def create(client, valid_payload):
    """Izveido iesniegumu un, ja vajag, iestata tā statusu."""

    def _create(status: str = "RECEIVED") -> dict:
        created = client.post("/submissions", json=valid_payload).json()
        if status != "RECEIVED":
            storage.update_status(created["id"], status)
        return created

    return _create


def withdraw(client, submission_id, reason=REASON):
    body = {} if reason is None else {"reason": reason}
    return client.post(f"/submissions/{submission_id}/withdraw", json=body)


def status_of(client, submission_id) -> str:
    return client.get(f"/submissions/{submission_id}").json()["status"]


def test_withdraw_received_returns_200(client, create):
    created = create("RECEIVED")

    response = withdraw(client, created["id"])

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "WITHDRAWN"
    assert data["dueDate"] == created["dueDate"]
    assert status_of(client, created["id"]) == "WITHDRAWN"


def test_withdraw_in_progress_returns_200(client, create):
    created = create("IN_PROGRESS")

    response = withdraw(client, created["id"])

    assert response.status_code == 200
    assert response.json()["status"] == "WITHDRAWN"


@pytest.mark.parametrize("status", ["ANSWERED", "WITHDRAWN", "FORWARDED"])
def test_withdraw_not_allowed_returns_409_and_status_unchanged(client, create, status):
    created = create(status)

    response = withdraw(client, created["id"])

    assert response.status_code == 409
    assert response.json() == {
        "error": {
            "code": "INVALID_STATE",
            "message": "Action not allowed in the current status",
        }
    }
    assert status_of(client, created["id"]) == status
    actions = [
        e["action"] for e in client.get(f"/submissions/{created['id']}/audit").json()
    ]
    assert "WITHDRAW" not in actions


def test_withdraw_twice_returns_409(client, create):
    created = create()

    assert withdraw(client, created["id"]).status_code == 200
    response = withdraw(client, created["id"])

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INVALID_STATE"


def test_withdraw_unknown_id_returns_404(client):
    response = withdraw(client, "IES-2026-999999")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


@pytest.mark.parametrize(
    ("reason", "issue"),
    [
        (None, "REQUIRED"),
        ("", "REQUIRED"),
        ("    ", "REQUIRED"),
        ("a" * 9, "INVALID_FORMAT"),
        ("   " + "a" * 9 + "   ", "INVALID_FORMAT"),
        ("a" * 501, "TOO_LONG"),
        (123, "INVALID_FORMAT"),
    ],
)
def test_withdraw_invalid_reason_returns_400(client, create, reason, issue):
    created = create()

    response = withdraw(client, created["id"], reason)

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["details"] == [{"field": "reason", "issue": issue}]
    assert status_of(client, created["id"]) == "RECEIVED"


@pytest.mark.parametrize("reason", ["a" * 10, "a" * 500, "  " + "a" * 500 + "  "])
def test_withdraw_reason_length_boundaries_accepted(client, create, reason):
    created = create()

    response = withdraw(client, created["id"], reason)

    assert response.status_code == 200


def test_withdraw_writes_audit_entry(client, create, monkeypatch):
    fixed = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(clock, "now", lambda: fixed)
    created = create()

    withdraw(client, created["id"], f"  {REASON}  ")
    response = client.get(f"/submissions/{created['id']}/audit")

    assert response.status_code == 200
    assert response.json() == [
        {"at": "2026-10-05T12:00:00Z", "action": "CREATE", "detail": None},
        {"at": "2026-10-05T12:00:00Z", "action": "WITHDRAW", "detail": REASON},
    ]


def test_withdraw_does_not_log_or_return_pii(client, create, valid_payload, caplog):
    caplog.set_level(logging.DEBUG)
    received = create()
    answered = create("ANSWERED")

    error_responses = [
        withdraw(client, received["id"], "short"),  # 400
        withdraw(client, "IES-2026-999999"),  # 404
        withdraw(client, answered["id"]),  # 409
    ]
    withdraw(client, received["id"])  # 200

    pii = [valid_payload[k] for k in ("personalCode", "fullName", "email", "body")]
    for response in error_responses:
        for value in pii:
            assert value not in response.text
    for value in pii:
        assert value not in caplog.text
