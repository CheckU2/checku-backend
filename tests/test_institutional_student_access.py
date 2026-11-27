from types import SimpleNamespace

import pytest
from bson import ObjectId
from fastapi import APIRouter, Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.config import get_settings
from app.dependencies import get_current_user
from app.routers.academic import attendance, classes, reports, sessions
from app.services.tenant_guard import legacy_only


@pytest.mark.parametrize(
    ("role", "allowed"),
    [
        ("student", True),
        ("teacher", True),
        ("tutor", True),
        ("career_admin", True),
        ("institution_admin", True),
        ("institution_owner", True),
        ("unknown", False),
    ],
)
def test_guard_uses_matched_templates_from_nested_fastapi_routers(role, allowed):
    academic = APIRouter()

    @academic.get("/classes/{class_id}/community")
    @academic.get("/other/classes/{class_id}/community")
    @academic.post("/classes/{class_id}/community")
    def community(class_id: str):
        return {"class_id": class_id}

    api = APIRouter()
    api.include_router(academic, dependencies=[Depends(legacy_only)])
    app = FastAPI()
    prefix = get_settings().api_v1_prefix
    app.include_router(api, prefix=prefix)
    app.dependency_overrides[get_current_user] = lambda: {"institution_id": "inst", "role": role}

    with TestClient(app) as client:
        response = client.get(f"{prefix}/classes/123/community")
        assert response.status_code == (200 if allowed else 403)
        if allowed:
            assert response.json() == {"class_id": "123"}
        assert client.get(f"{prefix}/other/classes/123/community").status_code == 403
        assert client.post(f"{prefix}/classes/123/community").status_code == 403


def make_request(method, path):
    request = Request({
        "type": "http",
        "method": method,
        "path": f"/api/v1{path}",
        "headers": [],
        "query_string": b"",
        "server": ("test", 80),
        "client": ("test", 1),
        "scheme": "http",
        "http_version": "1.1",
    })
    request.scope["route"] = SimpleNamespace(path=f"/api/v1{path}")
    return request


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/classes"),
        ("POST", "/classes/join"),
        ("GET", "/classes/{class_id}/community"),
        ("GET", "/attendance/me"),
        ("GET", "/justifications"),
        ("GET", "/justifications/options"),
        ("POST", "/justifications"),
        ("GET", "/justifications/{justification_id}/messages"),
        ("POST", "/justifications/{justification_id}/messages"),
        ("GET", "/reports/dashboard"),
    ],
)
def test_institutional_students_can_use_scoped_routes(method, path):
    student = {"role": "student", "account_type": "student", "institution_id": "inst"}

    assert legacy_only(make_request(method, path), student) is student


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/classes"),
        ("GET", "/classes/options"),
        ("POST", "/classes"),
        ("POST", "/classes/subjects"),
        ("GET", "/sessions"),
        ("POST", "/sessions"),
        ("POST", "/sessions/{session_id}/start"),
        ("GET", "/reports/dashboard"),
        ("GET", "/justifications"),
    ],
)
def test_institutional_teachers_can_use_scoped_work_routes(method, path):
    teacher = {"role": "teacher", "account_type": "teacher", "institution_id": "inst"}

    assert legacy_only(make_request(method, path), teacher) is teacher


@pytest.mark.parametrize(
    ("method", "path", "account"),
    [
        ("GET", "/classes/options", {"role": "student", "account_type": "student"}),
        ("POST", "/justifications/{justification_id}/approve", {"role": "student", "account_type": "student"}),
        ("POST", "/justifications", {"role": "teacher", "account_type": "teacher"}),
        ("POST", "/justifications/{justification_id}/approve", {"role": "teacher", "account_type": "teacher"}),
        ("GET", "/other/classes", {"role": "student", "account_type": "student"}),
        ("POST", "/classes/subjects", {"role": "institution_admin"}),
        ("POST", "/classes", {"role": "student", "account_type": "student"}),
        ("GET", "/reports/dashboard", {"role": "student", "account_type": "teacher"}),
        ("GET", "/groups", {"role": "teacher", "account_type": "teacher"}),
        ("POST", "/attendance/manual", {"role": "teacher", "account_type": "teacher"}),
    ],
)
def test_other_institutional_routes_remain_blocked(method, path, account):
    account["institution_id"] = "inst"

    with pytest.raises(HTTPException) as exc:
        legacy_only(make_request(method, path), account)

    assert exc.value.status_code == 403


def test_institutional_student_must_join_class_in_own_career_and_institution():
    career_id = ObjectId()
    institution_id = "institution-1"
    db = SimpleNamespace(careers=SimpleNamespace(find_one=lambda query: {
        "_id": career_id,
        "institution_id": institution_id,
    }))
    user = {
        "role": "student",
        "career_id": str(career_id),
        "institution_id": institution_id,
    }

    classes.ensure_student_class_scope(db, user, {"career_id": str(career_id)})

    with pytest.raises(HTTPException) as exc:
        classes.ensure_student_class_scope(db, user, {"career_id": str(ObjectId())})

    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "CAREER_MISMATCH"


def test_student_dashboard_metrics_query_only_own_records(monkeypatch):
    student_id = ObjectId()
    class_id = ObjectId()

    class Collection:
        def __init__(self, rows=None):
            self.rows = rows or []
            self.queries = []

        def find(self, query, projection=None):
            self.queries.append(query)
            return self.rows

        def count_documents(self, query):
            self.queries.append(query)
            return 2

    db = SimpleNamespace(
        class_memberships=Collection([{"class_id": class_id}]),
        class_sessions=Collection(),
        attendance=Collection(),
        justifications=Collection(),
    )
    monkeypatch.setattr(reports, "dbx", lambda: db)

    result = reports.dashboard({"_id": student_id, "role": "student"})

    assert result["data"] == {
        "sessions": 2,
        "active_sessions": 2,
        "attendance_records": 2,
        "pending_justifications": 2,
    }
    assert db.class_sessions.queries[0] == {"class_id": {"$in": [str(class_id)]}}
    assert db.attendance.queries == [{"user_id": student_id}]
    assert db.justifications.queries == [{
        "status": "pending",
        "$or": [{"student_user_id": student_id}, {"student_id": str(student_id)}],
    }]


def test_institutional_student_attendance_is_limited_to_own_career(monkeypatch):
    student_id = ObjectId()
    student_profile_id = ObjectId()
    career_id = ObjectId()
    class_id = ObjectId()
    session_id = ObjectId()

    class Cursor(list):
        def sort(self, *args):
            return self

        def skip(self, *args):
            return self

        def limit(self, *args):
            return self

    class Collection:
        def __init__(self, rows=None, document=None):
            self.rows = rows or []
            self.document = document
            self.queries = []

        def find_one(self, query, projection=None):
            return self.document

        def find(self, query, projection=None):
            self.queries.append(query)
            return Cursor(self.rows)

        def count_documents(self, query):
            self.queries.append(query)
            return 0

    db = SimpleNamespace(
        students=Collection([{"_id": student_profile_id}]),
        class_memberships=Collection([{"class_id": class_id}]),
        careers=Collection([{"_id": career_id}]),
        classes=Collection([{"_id": class_id}]),
        class_sessions=Collection([{"_id": session_id}]),
        attendance=Collection(),
    )
    monkeypatch.setattr(attendance, "dbx", lambda: db)

    result = attendance.my_attendance(page=1, limit=20, user={
        "_id": student_id,
        "role": "student",
        "institution_id": "institution-1",
        "career_id": str(career_id),
    })

    assert result["data"] == []
    assert db.classes.queries[0]["career_id"]["$in"] == [str(career_id), career_id]
    assert db.classes.queries[0]["_id"]["$in"] == [class_id]
    assert db.class_sessions.queries[0] == {"class_id": {"$in": [str(class_id), class_id]}}
    attendance_query = db.attendance.queries[0]
    assert attendance_query["$or"] == [
        {"student_id": {"$in": [str(student_id), student_id, str(student_profile_id), student_profile_id]}},
        {"user_id": {"$in": [str(student_id), student_id]}},
    ]
    assert attendance_query["session_id"]["$in"] == [str(session_id), session_id]


def test_institutional_teacher_class_and_session_queries_are_career_scoped():
    career_id = ObjectId()
    teacher_id = ObjectId()
    db = SimpleNamespace(
        careers=SimpleNamespace(find=lambda query, projection: [{"_id": career_id}]),
    )
    teacher = {
        "_id": teacher_id,
        "role": "teacher",
        "institution_id": "institution-1",
        "career_id": str(career_id),
    }

    class_query = classes.scope(db, teacher)
    session_query = sessions.teacher_session_query(db, teacher)
    expected_career_refs = [str(career_id), career_id]

    assert class_query["career_id"]["$in"] == expected_career_refs
    assert class_query["teacher_id"]["$in"] == [str(teacher_id), teacher_id]
    assert session_query["career_id"]["$in"] == expected_career_refs
    assert session_query["teacher_id"]["$in"] == [str(teacher_id), teacher_id]
