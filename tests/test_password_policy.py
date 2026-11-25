import pytest
from pydantic import ValidationError

from app.schemas.auth import ChangePasswordRequest, LoginRequest, RegisterRequest
from app.schemas.password_policy import password_policy_error
from app.schemas.user import UserCreate


@pytest.mark.parametrize("password", ["Abc!1de2", "Cl@veA7z", "Clave!9809", "Abc!12de", "Abc!١٢de", "Doce!9809abc", "Clave🔒xy"])
def test_new_password_is_accepted_across_creation_flows(password):
    assert password_policy_error(password) is None
    common = {"email": "test@example.com", "password": password, "full_name": "Test User"}
    assert RegisterRequest(**common).password == password
    assert UserCreate(**common, role="student").password == password
    assert ChangePasswordRequest(current_password="legacy1234", new_password=password).new_password == password


@pytest.mark.parametrize("password", ["Ab!1", "Abcdefg1", "Abcd!123", "Abcd!987", "A!123456789", "x" * 12 + "!"])
def test_invalid_passwords_are_rejected_but_existing_logins_are_compatible(password):
    assert password_policy_error(password)
    for schema, fields in (
        (RegisterRequest, {"email": "test@example.com", "password": password, "full_name": "Test User"}),
        (UserCreate, {"email": "test@example.com", "password": password, "full_name": "Test User", "role": "student"}),
        (ChangePasswordRequest, {"current_password": "old1234", "new_password": password}),
    ):
        with pytest.raises(ValidationError):
            schema(**fields)
    assert LoginRequest(email="test@example.com", password="old1234").password == "old1234"
