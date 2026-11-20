from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest
from bson import ObjectId
from fastapi import HTTPException

from app.routers.academic import generic, groups
from app.services import academic_catalogs


@pytest.mark.parametrize("start,end", [("", ""), ("2026-09-01", "2026-08-31"), ("fecha", "2026-12-31")])
def test_invalid_academic_period_dates_are_rejected(start, end):
    with pytest.raises(HTTPException) as exc:
        academic_catalogs.validate_period_dates({"start_date": start, "end_date": end})

    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "INVALID_PERIOD_DATES"


def test_valid_academic_period_dates_are_accepted():
    academic_catalogs.validate_period_dates({"start_date": "2026-09-01", "end_date": "2026-12-31"})
from app.services.authorization import apply_user_context
from app.services.institutions import is_institution_admin


def test_director_creates_group_in_assigned_career_only(monkeypatch):
    tenant, career, period = (str(ObjectId()) for _ in range(3))
    user = {'_id': ObjectId(), 'role': 'career_admin', 'institution_id': tenant, 'career_id': career}
    db = MagicMock()
    db.__getitem__.side_effect = lambda name: getattr(db, name)
    db.careers.find.return_value = [{'_id': ObjectId(career)}]
    db.groups.insert_one.return_value.inserted_id = ObjectId()
    monkeypatch.setattr(generic, 'get_database', lambda: db)
    create = next(route.endpoint for route in groups.router.routes if 'POST' in route.methods)

    result = create({'name': 'Grupo', 'period_id': period, 'institution_id': str(ObjectId())}, user)
    assert result['data']['career_id'] == career
    assert result['data']['institution_id'] == tenant
    assert db.careers.find.call_args.args[0]['institution_id'] == tenant
    assert db.academic_periods.find_one.call_args.args[0]['$or'][1] == {'institution_id': tenant, 'career_id': None}

    with pytest.raises(HTTPException, match='CAREER_MISMATCH') as exc:
        create({'name': 'Grupo', 'career_id': str(ObjectId()), 'period_id': period}, user)
    assert exc.value.status_code == 403

    db.academic_periods.find_one.return_value = None
    with pytest.raises(HTTPException, match='INVALID_PERIOD'):
        create({'name': 'Grupo', 'period_id': str(ObjectId())}, user)
    assert db.groups.insert_one.call_count == 1


def test_institutional_periods_are_readable_by_directors_but_writable_by_institution():
    tenant, career = (str(ObjectId()) for _ in range(2))
    db = SimpleNamespace(careers=Mock())
    db.careers.find.return_value = [{'_id': ObjectId(career)}]
    director = {'role': 'career_admin', 'institution_id': tenant, 'career_id': career}
    owner = {'role': 'institution_owner', 'institution_id': tenant}
    period = {'name': 'Otoño'}

    academic_catalogs.catalog_create_data(db, owner, 'academic_periods', period)
    assert period == {'name': 'Otoño', 'institution_id': tenant}
    assert {'institution_id': tenant, 'career_id': None} in academic_catalogs.catalog_scope(db, director, 'academic_periods')['$or']
    assert academic_catalogs.catalog_scope(db, director, 'academic_periods', write=True)['career_id'] == {
        '$in': [career, ObjectId(career)]
    }


def test_switching_institutions_drops_the_previous_career_and_permissions():
    tenant, other = (str(ObjectId()) for _ in range(2))
    user = {'_id': ObjectId(), 'role': 'staff', 'career_id': str(ObjectId()), 'institution_id': other}
    context = {'role': 'institution_admin', 'institution_id': tenant}
    actor = apply_user_context(user, context)
    db = SimpleNamespace(role_assignments=Mock())

    assert 'career_id' not in actor
    assert not is_institution_admin(db, actor, other)
    db.role_assignments.find_one.assert_not_called()
