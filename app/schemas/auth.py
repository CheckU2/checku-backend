
from pydantic import BaseModel, EmailStr, Field

from app.schemas.password_policy import NewPassword


class RegisterRequest(BaseModel):
    full_name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    password: NewPassword
    institution_id: str | None = None
    account_type: str = "student"
    career_id: str | None = None
    timezone: str | None = None


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class ResendVerificationRequest(BaseModel):
    email: EmailStr


class VerificationCodeRequest(BaseModel):
    code: str = Field(min_length=6, max_length=6, pattern=r"^[0-9]{6}$")


class VerifyEmailRequest(ResendVerificationRequest, VerificationCodeRequest):
    pass


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: NewPassword


class ChangeEmailRequest(BaseModel):
    email: EmailStr
    current_password: str = Field(min_length=1, max_length=128)


class TokenResponse(BaseModel):
    success: bool = True
    data: dict
