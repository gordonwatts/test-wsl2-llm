"""OpenCode credentials, configuration, and target command behavior."""

import hashlib
import io
import json
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest

from test_wsl2_llm.agents import get_agent_adapter
from test_wsl2_llm.config import build_config, load_config_file, save_config
from test_wsl2_llm.models import TestConfig, UsageRecord
from test_wsl2_llm.opencode import install_opencode, opencode_configuration, read_api_key
from test_wsl2_llm.pricing import load_and_calculate_costs
from test_wsl2_llm.runner import WindowsClient
from test_wsl2_llm.template import load_template_file


def _config(tmp_path: Path, **overrides: object) -> TestConfig:
    return TestConfig(
        prompt="answer",
        model="openai/gpt-5",
        output=str(tmp_path / "result"),
        agent="opencode",
        opencode_api_key_name="MY_OPENAI_KEY",
        **overrides,
    )


def test_host_dotenv_precedence_and_secret_not_in_serialized_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cwd = tmp_path / "cwd"
    home = tmp_path / "home"
    cwd.mkdir()
    home.mkdir()
    (cwd / ".env").write_text('MY_OPENAI_KEY="cwd-secret"\n', encoding="utf-8")
    (home / ".env").write_text("MY_OPENAI_KEY=home-secret\n", encoding="utf-8")
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(Path, "home", lambda: home)
    assert read_api_key("MY_OPENAI_KEY") == "cwd-secret"
    assert read_api_key("MY_OPENAI_KEY", str(home / ".env")) == "home-secret"
    config = _config(tmp_path)
    assert "cwd-secret" not in str(config.model_dump())
    assert config.model_dump()["opencode_api_key_name"] == "MY_OPENAI_KEY"


def test_dotenv_key_errors_do_not_echo_key_value(tmp_path: Path) -> None:
    source = tmp_path / ".env"
    source.write_text("MY_OPENAI_KEY=\nOTHER=secret", encoding="utf-8")
    with pytest.raises(ValueError, match="empty") as error:
        read_api_key("MY_OPENAI_KEY", str(source))
    assert "secret" not in str(error.value)


def test_configuration_converts_named_mcp_servers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import test_wsl2_llm.runner as runner

    monkeypatch.setattr(
        runner,
        "_load_mcp_servers",
        lambda names: {
            "local": {"command": "server", "args": ["--fast"], "env": {"A": "x"}},
            "remote": {"url": "https://example.test/mcp", "http_headers": {"X": "y"}},
        },
    )
    document = json.loads(
        opencode_configuration(
            _config(tmp_path, mcp_servers=["local", "remote"], plugins=["opencode-plugin"])
        )
    )
    assert document["mcp"]["local"] == {
        "type": "local",
        "command": ["server", "--fast"],
        "environment": {"A": "x"},
    }
    assert document["mcp"]["remote"]["headers"] == {"X": "y"}
    assert document["plugin"] == ["opencode-plugin"]


def test_cached_binary_reused_without_download(monkeypatch: pytest.MonkeyPatch) -> None:
    class Target:
        def login_bash(self, script: str, *args: str, check: bool = True):
            if script == "uname -sm":
                return subprocess.CompletedProcess([], 0, b"Linux x86_64\n", b"")
            assert script == 'test -x "$1"'
            assert args == ("/cache/opencode",)
            return subprocess.CompletedProcess([], 0, b"", b"")

        @staticmethod
        def text(result: subprocess.CompletedProcess[bytes]) -> str:
            return result.stdout.decode()

    monkeypatch.setattr(
        "urllib.request.urlopen", lambda *_args, **_kwargs: pytest.fail("downloaded")
    )
    assert install_opencode(Target(), "/cache") == "/cache/opencode"


@pytest.mark.parametrize(
    ("platform", "asset"),
    [
        ("Windows X64", "opencode-windows-x64.zip"),
        ("Windows ARM64", "opencode-windows-arm64.zip"),
        ("Linux x86_64", "opencode-linux-x64.tar.gz"),
    ],
)
def test_fresh_install_uses_verified_native_asset_and_atomic_stage(
    platform: str,
    asset: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = b"portable-cli"
    archive_stream = io.BytesIO()
    if asset.endswith(".zip"):
        with zipfile.ZipFile(archive_stream, "w") as package:
            package.writestr("opencode.exe", binary)
    else:
        with tarfile.open(fileobj=archive_stream, mode="w:gz") as package:
            info = tarfile.TarInfo("opencode")
            info.size = len(binary)
            package.addfile(info, io.BytesIO(binary))
    archive = archive_stream.getvalue()
    metadata = {
        "assets": [
            {
                "name": asset,
                "browser_download_url": "https://example.test/asset",
                "digest": "sha256:" + hashlib.sha256(archive).hexdigest(),
            }
        ]
    }
    requested: list[str] = []

    def urlopen(request: object, timeout: int):
        del timeout
        url = request.full_url if hasattr(request, "full_url") else str(request)
        requested.append(url)
        return io.BytesIO(json.dumps(metadata).encode() if "api.github.com" in url else archive)

    monkeypatch.setattr("urllib.request.urlopen", urlopen)

    class Target:
        files: dict[str, bytes] = {}
        staged: str | None = None

        def login_bash(self, script: str, *args: str, check: bool = True):
            del check
            if script == "uname -sm":
                return subprocess.CompletedProcess([], 0, platform.encode(), b"")
            if script == 'test -x "$1"':
                return subprocess.CompletedProcess([], 0 if args[0] in self.files else 1, b"", b"")
            raise AssertionError(script)

        @staticmethod
        def text(result: subprocess.CompletedProcess[bytes]) -> str:
            return result.stdout.decode()

        def write_file(self, path: str, content: bytes) -> None:
            self.staged = path
            self.files[path] = content

        def bash(self, script: str, *args: str, input_bytes: bytes | None = None):
            if script == 'chmod 755 -- "$1"':
                assert args[0] == self.staged
            elif script.startswith('if test -x "$2";'):
                self.files[args[1]] = self.files.pop(args[0])
            else:
                raise AssertionError(script)

    target = Target()
    expected = "/cache/opencode" + (".exe" if platform.startswith("Windows") else "")
    assert install_opencode(target, "/cache") == expected
    assert target.files == {expected: binary}
    assert target.staged is not None and target.staged.endswith(".tmp")
    assert requested == [
        "https://api.github.com/repos/anomalyco/opencode/releases/latest",
        "https://example.test/asset",
    ]
    assert install_opencode(target, "/cache") == expected
    assert len(requested) == 2


def test_template_key_name_roundtrips_without_a_value(tmp_path: Path) -> None:
    config = build_config(
        {
            "prompt": "hello",
            "model": "openai/gpt-5",
            "agent": "opencode",
            "opencode_api_key_name": "CUSTOM_KEY",
        },
        {"output": str(tmp_path / "result")},
    )
    path = tmp_path / "saved.yaml"
    save_config(config, path)
    assert load_config_file(path)["opencode_api_key_name"] == "CUSTOM_KEY"
    template = tmp_path / "template.yaml"
    template.write_text(
        "prompt_template: '{{ question }}'\nquestions:\n  - id: q1\n    question: hello\n"
        "agent: opencode\nmodel: openai/gpt-5\nopencode_api_key_name: CUSTOM_KEY\n",
        encoding="utf-8",
    )
    _, shared, _ = load_template_file(template)
    assert shared["opencode_api_key_name"] == "CUSTOM_KEY"


def test_openai_qualified_model_uses_only_openai_catalog_fallback() -> None:
    records = [
        UsageRecord(
            model="openai/gpt-6-luna", attribution="reported", input_tokens=100, output_tokens=10
        ),
        UsageRecord(
            model="other/gpt-6-luna", attribution="reported", input_tokens=100, output_tokens=10
        ),
    ]
    costs = load_and_calculate_costs(records, None).models
    assert costs[0].model == "openai/gpt-6-luna"
    assert costs[0].pricing_available is True
    assert costs[1].pricing_available is False
    assert costs[1].total_cost is None


def test_windows_launch_uses_named_key_file_and_prompt(tmp_path: Path) -> None:
    adapter = get_agent_adapter("opencode")
    adapter.configuration(_config(tmp_path))
    adapter._binary = "C:/opencode.exe"
    target = WindowsClient()
    command = adapter.command(
        target,
        home="C:/run/.harness/opencode-home",
        model="openai/gpt-5",
        reasoning_effort="high",
        workspace="C:/run/workspace",
    )
    assert command[0] == "powershell.exe"
    # The encoded script is UTF-16LE PowerShell; the secret value is never in it.
    import base64

    script = base64.b64decode(command[-1]).decode("utf-16-le")
    assert "$opencodeHome =" in script
    assert "api-key" in script and "inputs\\prompt.md" in script
    assert "--format json --auto" in script
    assert "MY_OPENAI_KEY" not in script
