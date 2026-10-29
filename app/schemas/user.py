from datetime import datetime
from typing import Literal
from pydantic import BaseModel,EmailStr,Field
Role=Literal["super_admin","career_admin","teacher","student","tutor"]
class UserCreate(BaseModel):
 email:EmailStr
 password:str=Field(min_length=12,max_length=128)
 role:Literal["career_admin","teacher","student","tutor"]
 full_name:str=Field(min_length=2,max_length=120)
 career_id:str|None=None
 employee_id:str|None=Field(default=None,max_length=40)
 department:str|None=Field(default=None,max_length=120)
class UserPublic(BaseModel):
 id:str
 email:EmailStr
 role:str
 full_name:str
 is_active:bool
 created_at:datetime
