from types import SimpleNamespace

import pytest
from bson import ObjectId
from fastapi import HTTPException

from app.services.authorization import apply_user_context, resolve_user_context, user_contexts


class Collection:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def find(self, query):
        return [
            row for row in self.rows
            if all(row.get(key) == value for key, value in query.items() if key != "user_id")
        ]

    def find_one(self, query):
        for row in self.rows:
            if all(row.get(key) == value for key, value in query.items()):
                return row
        return None


def institution_database(institution_id, assignments):
    institution_id = ObjectId(institution_id)
    return SimpleNamespace(
        institution_memberships=Collection([{
            "institution_id": str(institution_id),
            "user_id": "user-1",
            "status": "active",
        }]),
        institutions=Collection([{
            "_id": institution_id,
            "name": "Institución de prueba",
            "status": "active",
        }]),
        role_assignments=Collection(assignments),
    )


def test_contexts_include_only_active_assigned_roles():
    institution_id = str(ObjectId())
    db = institution_database(institution_id, [
        {
            "institution_id": institution_id,
            "user_id": "user-1",
            "role": "institution_owner",
            "scope_type": "institution",
            "scope_id": None,
            "status": "active",
        },
        {
            "institution_id": institution_id,
            "user_id": "user-1",
            "role": "teacher",
            "scope_type": "career",
            "scope_id": "career-1",
            "career_id": "career-1",
            "status": "revoked",
        },
    ])

    contexts = user_contexts(db, {"_id": "user-1", "role": "student", "institution_id": institution_id, "account_type": "staff"})

    assert [context["role"] for context in contexts] == ["institution_owner"]
    assert "staff.approve" in contexts[0]["permissions"]


def test_career_director_context_maps_to_legacy_scoped_role():
    institution_id = str(ObjectId())
    db = institution_database(institution_id, [{
        "institution_id": institution_id,
        "user_id": "user-1",
        "role": "career_director",
        "scope_type": "career",
        "scope_id": "career-1",
        "status": "active",
    }])
    user = {"_id": "user-1", "role": "student", "institution_id": institution_id}
    context = user_contexts(db, user)[0]

    scoped_user = apply_user_context(user, context)

    assert scoped_user["role"] == "career_admin"
    assert scoped_user["career_id"] == "career-1"
    assert scoped_user["active_context"]["role"] == "career_director"


def test_cannot_select_unassigned_context():
    institution_id = str(ObjectId())
    db = institution_database(institution_id, [])

    with pytest.raises(HTTPException) as exc:
        resolve_user_context(db, {"_id": "user-1", "role": "student", "institution_id": institution_id, "account_type": "staff"}, {
            "institution_id": institution_id,
            "role": "institution_owner",
            "scope_type": "institution",
            "scope_id": None,
        })

    assert exc.value.status_code == 403
