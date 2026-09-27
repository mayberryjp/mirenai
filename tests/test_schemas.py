import pytest
from pydantic import ValidationError

from mirenai.api.schemas import ClientModeUpdate, HostUpdate


def test_client_mode_accepts_allow_and_block() -> None:
    assert ClientModeUpdate.model_validate({"mode": "forward"}).mode == "forward"
    assert ClientModeUpdate.model_validate({"mode": "deny"}).mode == "deny"


@pytest.mark.parametrize("mode", ["blocklist", "default", "override", "allow", "nope"])
def test_client_mode_rejects_everything_else(mode: str) -> None:
    with pytest.raises(ValidationError):
        ClientModeUpdate.model_validate({"mode": mode})


def test_host_update_icon_is_trimmed() -> None:
    assert HostUpdate.model_validate({"icon": "  tv_icon  "}).icon == "tv_icon"


def test_host_update_blank_icon_becomes_none() -> None:
    assert HostUpdate.model_validate({"icon": "   "}).icon is None


def test_host_update_icon_too_long_rejected() -> None:
    with pytest.raises(ValidationError):
        HostUpdate.model_validate({"icon": "x" * 256})


def test_host_update_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        HostUpdate.model_validate({"nope": "x"})
