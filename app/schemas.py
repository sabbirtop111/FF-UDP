# -*- coding: utf-8 -*-
"""
Pydantic v2 Validation Schemas for API requests and responses.
"""

import re
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field, field_validator, model_validator

VALID_REGIONS = {
    "ME", "IND", "BD", "SG", "TH", "PH", "VN", "MY", "ID",
    "HK", "TW", "PK", "CIS", "BR", "NA", "SAC", "EUROPE"
}

UID_REGEX = re.compile(r"^[A-Za-z0-9_\-]{3,64}$")


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=128)


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(..., min_length=1, max_length=128)
    new_password: str = Field(..., min_length=12, max_length=128)


class AccountCreateRequest(BaseModel):
    auth_type: str = Field(default="uid_password", description="uid_password or access_token")
    uid: Optional[str] = Field(default=None, max_length=64, description="Guest/Login UID; required for UID/password login")
    password: Optional[str] = Field(default=None, max_length=256, description="Account password; required for UID/password login")
    access_token: Optional[str] = Field(default=None, max_length=8192, description="Manually supplied Access Token; required for token login")
    display_name: Optional[str] = Field(default=None, max_length=80)
    region: str = Field(default="ME", max_length=16)
    mode_id: int = Field(default=1, ge=1, le=20)
    map_id: int = Field(default=1, ge=1, le=20)
    notes: Optional[str] = Field(default="", max_length=500)

    @field_validator("auth_type")
    @classmethod
    def validate_auth_type(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in {"uid_password", "access_token"}:
            raise ValueError("Authentication method must be uid_password or access_token.")
        return v

    @field_validator("uid", mode="before")
    @classmethod
    def validate_uid(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = str(v).strip()
        if not v:
            return None
        if not UID_REGEX.match(v):
            raise ValueError("UID must be 3-64 alphanumeric characters, hyphens, or underscores.")
        return v

    @field_validator("password", "access_token", mode="before")
    @classmethod
    def strip_secrets(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        value = str(v).strip()
        return value or None

    @model_validator(mode="after")
    def validate_auth_credentials(self):
        if self.auth_type == "uid_password":
            if not self.uid or not self.password or len(self.password) < 3:
                raise ValueError("UID and a password of at least 3 characters are required for UID/password login.")
        elif not self.access_token:
            raise ValueError("An Access Token is required for Access Token login.")
        return self

    @field_validator("region")
    @classmethod
    def validate_region(cls, v: str) -> str:
        v = v.strip().upper()
        if v not in VALID_REGIONS:
            raise ValueError(f"Unsupported region '{v}'. Valid regions: {', '.join(sorted(VALID_REGIONS))}")
        return v


class BulkAccountCreateRequest(BaseModel):
    raw_lines: str = Field(..., min_length=3, max_length=50000, description="Lines of UID:PASSWORD or UID,PASSWORD,REGION,DISPLAY_NAME")
    default_region: str = Field(default="ME")

    @field_validator("default_region")
    @classmethod
    def validate_default_region(cls, v: str) -> str:
        v = v.strip().upper()
        if v not in VALID_REGIONS:
            raise ValueError(f"Unsupported region '{v}'.")
        return v


class AccountUpdateRequest(BaseModel):
    auth_type: Optional[str] = Field(default=None, description="uid_password or access_token")
    uid: Optional[str] = Field(default=None, max_length=64, description="Guest UID; required when switching to UID/password login")
    display_name: Optional[str] = Field(default=None, min_length=1, max_length=80)
    password: Optional[str] = Field(default=None, max_length=256, description="Leave empty to keep the encrypted password")
    access_token: Optional[str] = Field(default=None, max_length=8192, description="Leave empty to keep the encrypted Access Token")
    region: Optional[str] = Field(default=None, max_length=16)
    mode_id: Optional[int] = Field(default=None, ge=1, le=20)
    map_id: Optional[int] = Field(default=None, ge=1, le=20)
    notes: Optional[str] = Field(default=None, max_length=500)

    @field_validator("uid", mode="before")
    @classmethod
    def validate_uid(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        value = str(v).strip()
        if not value:
            return None
        if not UID_REGEX.match(value):
            raise ValueError("UID must be 3-64 alphanumeric characters, hyphens, or underscores.")
        return value

    @field_validator("auth_type")
    @classmethod
    def validate_auth_type(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip().lower()
        if v not in {"uid_password", "access_token"}:
            raise ValueError("Authentication method must be uid_password or access_token.")
        return v

    @field_validator("password", "access_token", mode="before")
    @classmethod
    def strip_optional_secrets(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        value = str(v).strip()
        return value or None

    @field_validator("region")
    @classmethod
    def validate_region(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip().upper()
        if v not in VALID_REGIONS:
            raise ValueError(f"Unsupported region '{v}'.")
        return v


class BulkActionRequest(BaseModel):
    account_ids: List[str] = Field(..., min_length=1, max_length=1000)
    action: str = Field(..., description="One of: start, stop, restart, delete, change_region, clear_errors, reset_daily")
    region: Optional[str] = Field(default=None)

    @field_validator("action")
    @classmethod
    def validate_action(cls, v: str) -> str:
        allowed = {"start", "stop", "restart", "delete", "change_region", "clear_errors", "reset_daily"}
        v = v.strip().lower()
        if v not in allowed:
            raise ValueError(f"Invalid bulk action '{v}'. Allowed: {', '.join(sorted(allowed))}")
        return v

    @field_validator("region")
    @classmethod
    def validate_region(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip().upper()
        if v not in VALID_REGIONS:
            raise ValueError(f"Unsupported region '{v}'.")
        return v


class MetadataImportItem(BaseModel):
    uid: str
    display_name: Optional[str] = None
    region: Optional[str] = "ME"
    password: Optional[str] = None
    mode_id: Optional[int] = 1
    map_id: Optional[int] = 1
    notes: Optional[str] = ""


class MetadataImportRequest(BaseModel):
    accounts: List[Dict[str, Any]] = Field(..., min_length=1, max_length=500)
    default_region: str = Field(default="ME")
