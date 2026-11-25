import re
import unicodedata
from typing import Annotated

from pydantic import AfterValidator, Field


CONSECUTIVE_DIGITS = re.compile(r'012|123|234|345|456|567|678|789|987|876|765|654|543|432|321|210')


def password_policy_error(password: object) -> str | None:
    if not isinstance(password, str) or not 8 <= len(password) <= 12:
        return "La contraseña debe tener entre 8 y 12 caracteres."
    if not any(unicodedata.category(char)[0] in {"P", "S"} for char in password):
        return "Agrega al menos un carácter especial, como !, @ o #."
    if CONSECUTIVE_DIGITS.search(password):
        return "Evita secuencias de tres o más números, como 123 o 987."
    return None


def validate_new_password(password: str) -> str:
    error = password_policy_error(password)
    if error:
        raise ValueError(error)
    return password


NewPassword = Annotated[str, Field(min_length=8, max_length=12), AfterValidator(validate_new_password)]
