"""Shared validation for required registration/admin contact numbers."""
import re
from typing import Annotated
from pydantic import BeforeValidator, Field


def normalize_phone(value):
    if not isinstance(value, str):
        raise ValueError('请填写联系电话')
    value = value.strip()
    if not re.fullmatch(r'\+?[0-9 ()-]+', value):
        raise ValueError('联系电话仅支持数字、区号及常用分隔符')
    return re.sub(r'[ ()-]', '', value)


PhoneNumber = Annotated[str, BeforeValidator(normalize_phone),
    Field(min_length=7, max_length=16, pattern=r'^\+?[0-9]{7,15}$')]
