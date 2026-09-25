"""Pydantic request models for API validation (system boundary)."""

from __future__ import annotations

from ipaddress import ip_address
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from mirenai.domain.policy import VALID_ACTIONS, WILDCARD

_VALID_PROTOCOLS = {"udp", "tcp"}
_VALID_DEFAULT_ACTIONS = {"deny", "forward"}


def _check_client(value: str) -> str:
    if value != WILDCARD:
        ip_address(value)  # raises ValueError for bad addresses
    return value


def _check_port(value: int) -> int:
    if not 1 <= value <= 65535:
        raise ValueError("port must be between 1 and 65535")
    return value


def _check_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("url must be an http(s) URL")
    return value


def _check_interval_hours(value: int) -> int:
    if value < 1:
        raise ValueError("update_interval_hours must be at least 1")
    return value


class PolicyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client: str
    domain: str
    action: str
    override_response: str | None = None
    override_ttl: int = 300
    enabled: bool = True
    description: str | None = None

    @field_validator("client")
    @classmethod
    def _validate_client(cls, value: str) -> str:
        return _check_client(value)

    @field_validator("domain")
    @classmethod
    def _validate_domain(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("domain must not be empty")
        return value

    @field_validator("action")
    @classmethod
    def _validate_action(cls, value: str) -> str:
        if value not in VALID_ACTIONS:
            raise ValueError(f"action must be one of {sorted(VALID_ACTIONS)}")
        return value

    @model_validator(mode="after")
    def _override_requires_response(self) -> PolicyCreate:
        if self.action == "override" and not (self.override_response or "").strip():
            raise ValueError("override_response is required when action is 'override'")
        return self


class PolicyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client: str | None = None
    domain: str | None = None
    action: str | None = None
    override_response: str | None = None
    override_ttl: int | None = None
    enabled: bool | None = None
    description: str | None = None

    @field_validator("client")
    @classmethod
    def _validate_client(cls, value: str | None) -> str | None:
        return _check_client(value) if value is not None else value

    @field_validator("action")
    @classmethod
    def _validate_action(cls, value: str | None) -> str | None:
        if value is not None and value not in VALID_ACTIONS:
            raise ValueError(f"action must be one of {sorted(VALID_ACTIONS)}")
        return value


class UpstreamCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    address: str
    port: int = 53
    protocol: str = "udp"
    enabled: bool = True
    priority: int = 100

    @field_validator("address")
    @classmethod
    def _validate_address(cls, value: str) -> str:
        ip_address(value)
        return value

    @field_validator("port")
    @classmethod
    def _validate_port(cls, value: int) -> int:
        return _check_port(value)

    @field_validator("protocol")
    @classmethod
    def _validate_protocol(cls, value: str) -> str:
        if value not in _VALID_PROTOCOLS:
            raise ValueError(f"protocol must be one of {sorted(_VALID_PROTOCOLS)}")
        return value


class UpstreamUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    address: str | None = None
    port: int | None = None
    protocol: str | None = None
    enabled: bool | None = None
    priority: int | None = None

    @field_validator("address")
    @classmethod
    def _validate_address(cls, value: str | None) -> str | None:
        if value is not None:
            ip_address(value)
        return value

    @field_validator("port")
    @classmethod
    def _validate_port(cls, value: int | None) -> int | None:
        return _check_port(value) if value is not None else value

    @field_validator("protocol")
    @classmethod
    def _validate_protocol(cls, value: str | None) -> str | None:
        if value is not None and value not in _VALID_PROTOCOLS:
            raise ValueError(f"protocol must be one of {sorted(_VALID_PROTOCOLS)}")
        return value


class SettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cache_enabled: bool | None = None
    cache_max_ttl: int | None = None
    cache_min_ttl: int | None = None
    cache_max_entries: int | None = None
    forward_timeout: float | None = None
    default_action: str | None = None
    refresh_seconds: int | None = None
    query_flush_seconds: int | None = None
    log_queries: bool | None = None

    @field_validator("default_action")
    @classmethod
    def _validate_default_action(cls, value: str | None) -> str | None:
        if value is not None and value not in _VALID_DEFAULT_ACTIONS:
            raise ValueError(f"default_action must be one of {sorted(_VALID_DEFAULT_ACTIONS)}")
        return value


class BlocklistCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    url: str
    update_interval_hours: int = 24
    enabled: bool = True

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name must not be empty")
        return value

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        return _check_url(value)

    @field_validator("update_interval_hours")
    @classmethod
    def _validate_interval(cls, value: int) -> int:
        return _check_interval_hours(value)


class BlocklistUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    url: str | None = None
    update_interval_hours: int | None = None
    enabled: bool | None = None

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("name must not be empty")
        return value

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str | None) -> str | None:
        return _check_url(value) if value is not None else value

    @field_validator("update_interval_hours")
    @classmethod
    def _validate_interval(cls, value: int | None) -> int | None:
        return _check_interval_hours(value) if value is not None else value
