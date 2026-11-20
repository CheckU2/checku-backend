from types import SimpleNamespace

import pytest
from bson import ObjectId
from fastapi import HTTPException

from app.routers import institutions as routes


class Collection:
    def __init__(self, *, document=None, modified_count=1):
        self.document = document
        self.modified_count = modified_count
        self.updates = []
        self.inserts = []

    def find_one(self, query, projection=None):
        return self.document

    def update_one(self, query, update, upsert=False):
        self.updates.append((query, update, upsert))
        return SimpleNamespace(modified_count=self.modified_count, matched_count=self.modified_count)

    def insert_one(self, document):
        self.inserts.append(document)
        return SimpleNamespace(inserted_id=ObjectId())

    def count_documents(self, query):
        return self.modified_count


def test_platform_can_assign_existing_user_as_institution_owner(monkeypatch):
    institution_id = str(ObjectId())
    account = {
        "_id": ObjectId(),
        "email": "owner@example.com",
        "full_name": "Propietario",
        "role": "student",
        "is_active": True,
    }
    db = SimpleNamespace(
        users=Collection(document=account),
        role_assignments=Collection(),
        institution_memberships=Collection(),
        audit_logs=Collection(),
    )
    monkeypatch.setattr(routes, "db_required", lambda: db)
    monkeypatch.setattr(routes, "check_platform", lambda user: None)
    monkeypatch.setattr(routes, "institution", lambda *args, **kwargs: {})
    monkeypatch.setattr(routes, "public_user", lambda user: {"id": str(user["_id"]), "email": user["email"]})

    result = routes.assign_institution_owner(
        institution_id,
        {"email": "OWNER@example.com"},
        {"_id": ObjectId(), "role": "super_admin"},
    )

    assert result["data"]["email"] == "owner@example.com"
    assert db.institution_memberships.updates[0][1]["$set"]["status"] == "active"
    assignment_update = db.role_assignments.updates[0][1]["$set"]
    assert assignment_update["status"] == "active"
    assert db.audit_logs.inserts[0]["action"] == "assign_owner"


def test_approval_activates_membership_and_teacher_role(monkeypatch):
    institution_id = str(ObjectId())
    request_id = ObjectId()
    target_id = ObjectId()
    request = {
        "_id": request_id,
        "user_id": str(target_id),
        "institution_id": institution_id,
        "requested_role": "teacher",
        "scope_type": "career",
        "scope_id": str(ObjectId()),
        "status": "pending",
    }
    target = {"_id": target_id, "is_active": True, "account_type": "teacher"}
    db = SimpleNamespace(
        role_requests=Collection(document=request),
        users=Collection(document=target),
        institution_memberships=Collection(),
        role_assignments=Collection(),
        teachers=Collection(),
        audit_logs=Collection(),
    )
    monkeypatch.setattr(routes, "db_required", lambda: db)
    monkeypatch.setattr(routes, "institution", lambda *args, **kwargs: {})
    monkeypatch.setattr(routes, "can_review_requests", lambda *args, **kwargs: True)
    monkeypatch.setattr(routes, "institution_career", lambda *args: {"_id": request["scope_id"]})
    reviewer = {"_id": ObjectId(), "role": "institution_owner"}

    result = routes.approve_role_request(institution_id, str(request_id), reviewer)

    assert result["data"]["status"] == "approved"
    assert db.role_requests.updates[0][1]["$set"]["status"] == "approved"
    assert db.institution_memberships.updates[0][1]["$set"]["status"] == "active"
    teacher_assignment = db.role_assignments.updates[0][1]["$set"]
    assert teacher_assignment["status"] == "active"
    assert teacher_assignment["career_id"] == request["scope_id"]
    profile_filter, profile_update, _ = db.teachers.updates[0]
    assert profile_filter["institution_id"] == {"$in": [institution_id, None]}
    assert profile_filter["career_id"] == request["scope_id"]
    assert profile_update["$set"]["career_id"] == request["scope_id"]
    assert db.users.updates[0][1]["$set"]["account_type"] == "teacher"


def test_simultaneous_approval_loser_does_not_grant_role(monkeypatch):
    institution_id = str(ObjectId())
    request_id = ObjectId()
    target_id = ObjectId()
    request = {
        "_id": request_id,
        "user_id": str(target_id),
        "institution_id": institution_id,
        "requested_role": "teacher",
        "scope_type": "institution",
        "scope_id": None,
        "status": "pending",
    }
    target = {"_id": target_id, "is_active": True}
    db = SimpleNamespace(
        role_requests=Collection(document=request, modified_count=0),
        users=Collection(document=target),
        institution_memberships=Collection(),
        role_assignments=Collection(),
        teachers=Collection(),
        audit_logs=Collection(),
    )
    monkeypatch.setattr(routes, "db_required", lambda: db)
    monkeypatch.setattr(routes, "institution", lambda *args, **kwargs: {})
    monkeypatch.setattr(routes, "can_review_requests", lambda *args, **kwargs: True)

    with pytest.raises(HTTPException) as exc:
        routes.approve_role_request(institution_id, str(request_id), {"_id": ObjectId(), "role": "institution_owner"})

    assert exc.value.status_code == 409
    assert not db.role_assignments.updates
    assert not db.teachers.updates


def test_institution_admin_can_revoke_role_and_audit(monkeypatch):
    institution_id = str(ObjectId())
    target_id = str(ObjectId())
    db = SimpleNamespace(
        role_assignments=Collection(),
        audit_logs=Collection(),
    )
    monkeypatch.setattr(routes, "db_required", lambda: db)
    monkeypatch.setattr(routes, "institution", lambda *args, **kwargs: {})
    monkeypatch.setattr(routes, "is_institution_admin", lambda *args, **kwargs: True)
    actor = {"_id": ObjectId(), "role": "institution_owner", "institution_id": institution_id}

    result = routes.revoke_role(
        institution_id,
        {"user_id": target_id, "role": "teacher", "scope_type": "career", "scope_id": str(ObjectId())},
        actor,
    )

    assert result["data"]["status"] == "revoked"
    assert db.role_assignments.updates[0][0]["status"] == "active"
    assert db.role_assignments.updates[0][1]["$set"]["revoked_by"] == str(actor["_id"])
    assert db.audit_logs.inserts[0]["action"] == "revoke_role"


def test_platform_cannot_revoke_the_only_institution_owner(monkeypatch):
    institution_id = str(ObjectId())
    db = SimpleNamespace(
        role_assignments=Collection(modified_count=1),
        audit_logs=Collection(),
    )
    monkeypatch.setattr(routes, "db_required", lambda: db)
    monkeypatch.setattr(routes, "institution", lambda *args, **kwargs: {})

    with pytest.raises(HTTPException) as exc:
        routes.revoke_role(
            institution_id,
            {"user_id": str(ObjectId()), "role": "institution_owner", "scope_type": "institution", "scope_id": None},
            {"_id": ObjectId(), "role": "super_admin"},
        )

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "LAST_OWNER"
    assert not db.role_assignments.updates
