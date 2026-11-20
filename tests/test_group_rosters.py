from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from bson import ObjectId
from fastapi import HTTPException
from pymongo.errors import DuplicateKeyError

from app.routers.academic import career_management, classes


def test_generated_join_code_contains_six_alphanumeric_characters():
    code = classes.generate_join_code()

    assert len(code) == 6
    assert all(character in classes.GENERATED_CODE_CHARACTERS for character in code)
    assert any(character.isalpha() for character in code)
    assert any(character.isdigit() for character in code)


def test_institutional_tutor_scope_comes_from_active_group_assignments():
    career, group = ObjectId(), ObjectId()
    db = SimpleNamespace(careers=Mock(), role_assignments=Mock())
    db.role_assignments.find.return_value = [{"scope_id": str(group)}]
    user = {
        '_id': ObjectId(), 'role': 'tutor', 'career_id': str(career),
        'institution_id': str(ObjectId()),
        'active_context': {'scope_type': 'group', 'scope_id': str(group)},
    }

    query = classes.scope(db, user)
    assignment_filter = db.role_assignments.find.call_args.args[0]
    assert assignment_filter["institution_id"] == user["institution_id"]
    assert assignment_filter["user_id"]["$in"] == [str(user["_id"]), user["_id"]]
    assert assignment_filter["status"] == "active"

    assert query['group_id'] == {'$in': [str(group), group]}
    assert query['career_id'] == {'$in': [str(career), career]}
    assert 'tutor_ids' not in query
    assert db.careers.find_one.call_args.args[0]['institution_id'] == user['institution_id']
    db.role_assignments.find.return_value = []
    assert classes.scope(db, user)['group_id'] == {'$in': []}


def test_group_roster_uses_class_memberships_without_student_profiles(monkeypatch):
    career, group, enrolled_class, student = [ObjectId() for _ in range(4)]
    db = SimpleNamespace(
        groups=Mock(), students=Mock(), classes=Mock(), class_memberships=Mock(), users=Mock()
    )
    db.groups.find_one.return_value = {'_id': group}
    db.students.find.return_value = []
    db.classes.find.return_value = [{'_id': enrolled_class}]
    db.class_memberships.find.return_value = [{'student_id': str(student)}, {'student_id': student}]
    db.users.find.return_value.sort.return_value = [{'_id': student, 'full_name': 'Ana'}]
    monkeypatch.setattr(career_management, 'get_database', lambda: db)
    admin = {'role': 'career_admin', 'career_id': str(career)}

    result = career_management.group_members(str(group), admin)

    assert [row['id'] for row in result['data']['students']] == [str(student)]
    assert db.classes.find.call_args.args[0]['group_id'] == {'$in': [str(group), group]}
    assert db.class_memberships.find.call_args.args[0]['class_id'] == {
        '$in': [str(enrolled_class), enrolled_class]
    }
    assert db.users.find.call_args.args[0]['$or'][1]['_id'] == {'$in': [student]}


@pytest.mark.parametrize('historical_membership', [True, False])
@pytest.mark.parametrize('code,normalized', [('123456', '123456'), (' ab12cd ', 'AB12CD')])
def test_join_rejects_historical_and_concurrent_duplicates(monkeypatch, historical_membership, code, normalized):
    student, enrolled_class = ObjectId(), ObjectId()
    db = SimpleNamespace(classes=Mock(), class_memberships=Mock(), audit_logs=Mock())
    db.classes.find_one.return_value = {
        '_id': enrolled_class, 'subject_id': ObjectId(), 'period_id': ObjectId(),
    }
    db.class_memberships.find.return_value = [{'class_id': str(enrolled_class)}] if historical_membership else []
    db.class_memberships.update_one.side_effect = DuplicateKeyError('duplicate membership')
    monkeypatch.setattr(classes, 'get_database', lambda: db)

    with pytest.raises(HTTPException) as exc:
        classes.join_class({'code': code}, {'_id': student, 'role': 'student'})

    assert exc.value.status_code == 409
    assert exc.value.detail['code'] == 'ALREADY_ENROLLED'
    assert db.classes.find_one.call_args.args[0]['join_code'] == normalized
    assert db.class_memberships.find.call_args.args[0] == {'student_id': {'$in': [str(student), student]}}
    assert db.class_memberships.update_one.call_count == (0 if historical_membership else 1)
    db.audit_logs.insert_one.assert_not_called()
