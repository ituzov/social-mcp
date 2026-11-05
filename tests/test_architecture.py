import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("module", "forbidden_import"),
    [
        ("threads/tools.py", "from social_mcp.accounts.repository import get_account"),
        (
            "threads/tools.py",
            "from social_mcp.threads.connection.page import ConnectionPage",
        ),
        ("app.py", "from social_mcp.threads.client import fetch_profile"),
        ("accounts/schemas.py", "from social_mcp.threads import ThreadsService"),
        ("accounts/schemas.py", "from social_mcp.accounts.models import AccountRecord"),
        (
            "threads/client/posts.py",
            "from social_mcp.threads.tools import register_threads_tools",
        ),
        (
            "threads/service.py",
            "from social_mcp.threads.client.posts import fetch_posts",
        ),
        (
            "threads/client/auth.py",
            "from social_mcp.threads.client.posts import fetch_posts",
        ),
        (
            "threads/client/http.py",
            "from social_mcp.threads.client.profile import fetch_profile",
        ),
        ("updates.py", "from mcp.server import MCPServer"),
        ("updates.py", "from social_mcp.accounts import get_account"),
        ("threads/service.py", "from mcp.server import MCPServer"),
        ("threads/service.py", "from social_mcp.mcp_server import SocialMCPServer"),
    ],
)
def test_import_contract_detects_one_isolated_violation(
    tmp_path, module, forbidden_import
):
    shutil.copytree(
        ROOT / "src", tmp_path / "src", ignore=shutil.ignore_patterns("__pycache__")
    )
    shutil.copy2(ROOT / "pyproject.toml", tmp_path / "pyproject.toml")
    path = tmp_path / "src/social_mcp" / module
    path.write_text(
        path.read_text(encoding="utf-8") + "\n" + forbidden_import + "\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [str(Path(sys.executable).parent / "lint-imports"), "--no-cache", "--no-logo"],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(tmp_path / "src")},
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "BROKEN" in result.stdout
