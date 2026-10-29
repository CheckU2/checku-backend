import pytest
from app.services.policies import attendance_percentage, derived_absences_from_confirmed_abandonments, is_at_risk

@pytest.mark.parametrize(("count","expected"), [(0,0),(1,0),(2,0),(3,1),(5,1),(6,2),(8,2),(9,3)])
def test_abandonments_are_derived_deterministically(count,expected):
    assert derived_absences_from_confirmed_abandonments(count)==expected

def test_attendance_percentage_and_risk():
    assert attendance_percentage(present=7,late=0,excused=1,evaluable_sessions=10)==80.0
    assert not is_at_risk(80.0)
    assert is_at_risk(79.99)
    assert attendance_percentage(present=0,late=0,excused=0,evaluable_sessions=0) is None

def test_negative_counts_rejected():
    with pytest.raises(ValueError):
        derived_absences_from_confirmed_abandonments(-1)
