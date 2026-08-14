from unittest.mock import MagicMock, patch

import pytest

from src.exceptions import ValidationError
from src.tools.core_tools import _scan_repo_via_daemon


def test_scan_repo_via_daemon_uses_read_only_networkless_helper():
    client = MagicMock()
    client.containers.run.return_value = b"2097152 37\n"

    with (
        patch("src.tools.core_tools.docker.from_env", return_value=client),
        patch(
            "src.tools.core_tools._joern_helper_image",
            return_value="sha256:helper",
        ),
    ):
        result = _scan_repo_via_daemon("/mnt/c/work/project")

    assert result == (2, 37)
    call = client.containers.run.call_args
    assert call.kwargs["image"] == "sha256:helper"
    assert call.kwargs["volumes"] == {"/mnt/c/work": {"bind": "/src", "mode": "ro"}}
    assert call.kwargs["network_disabled"] is True
    assert call.kwargs["remove"] is True
    assert "cd /src/project" in call.kwargs["entrypoint"][2]
    assert "node_modules" in call.kwargs["entrypoint"][2]


def test_scan_repo_via_daemon_rejects_invalid_helper_output():
    client = MagicMock()
    client.containers.run.return_value = b"not-a-size\n"

    with (
        patch("src.tools.core_tools.docker.from_env", return_value=client),
        patch(
            "src.tools.core_tools._joern_helper_image",
            return_value="sha256:helper",
        ),
        pytest.raises(ValidationError, match="Invalid size result"),
    ):
        _scan_repo_via_daemon("/mnt/c/work/project")
