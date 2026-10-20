"""OpenCode host credentials and configuration translation."""

from __future__ import annotations

import hashlib
import json
import re
import tarfile
import tempfile
import urllib.request
import uuid
import zipfile
from pathlib import Path
from typing import Any

from test_wsl2_llm.models import TestConfig

_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def read_api_key(name: str | None, source: str | None = None) -> str:
    """Read one named key from the invocation host, without returning its path."""
    if not name or not _ENV_NAME.fullmatch(name):
        raise ValueError("OpenCode requires --opencode-api-key-name with a valid variable name")
    candidates = (
        [Path(source).expanduser()] if source else [Path.cwd() / ".env", Path.home() / ".env"]
    )
    for path in candidates:
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            matched = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)", line)
            if not matched or matched.group(1) != name:
                continue
            value = matched.group(2).strip()
            if value[:1] in {"'", '"'}:
                quote = value[0]
                if len(value) < 2 or value[-1] != quote:
                    raise ValueError(f"OpenCode key {name} has malformed quoting in {path}")
                value = value[1:-1]
            else:
                value = value.split(" #", 1)[0].strip()
            if not value:
                raise ValueError(f"OpenCode key {name} is empty in {path}")
            return value
    locations = ", ".join(str(path) for path in candidates)
    raise FileNotFoundError(f"OpenCode key {name} was not found in {locations}")


def opencode_configuration(config: TestConfig) -> str:
    """Build an isolated OpenCode config from selected host MCP definitions."""
    from test_wsl2_llm.runner import _load_mcp_servers

    document: dict[str, Any] = {"$schema": "https://opencode.ai/config.json"}
    servers = _load_mcp_servers(config.mcp_servers)
    if servers:
        converted: dict[str, Any] = {}
        for name, entry in servers.items():
            if entry.get("enabled") is False:
                raise ValueError(f"OpenCode MCP server '{name}' is disabled in the source config")
            command = entry.get("command")
            url = entry.get("url")
            if command is not None:
                unknown = set(entry) - {"command", "args", "env", "enabled"}
                if unknown:
                    raise ValueError(
                        f"OpenCode MCP server '{name}' has unsupported settings: "
                        + ", ".join(sorted(unknown))
                    )
                if not isinstance(command, str) or not command:
                    raise ValueError(f"OpenCode MCP server '{name}' needs a command")
                args = entry.get("args", [])
                if not isinstance(args, list) or any(not isinstance(arg, str) for arg in args):
                    raise ValueError(f"OpenCode MCP server '{name}' has invalid args")
                settings: dict[str, Any] = {"type": "local", "command": [command, *args]}
                environment = entry.get("env")
                if environment:
                    settings["environment"] = environment
            elif isinstance(url, str) and url:
                if entry.get("env_http_headers") or entry.get("bearer_token_env_var"):
                    raise ValueError(
                        f"OpenCode MCP server '{name}' uses host environment header variables "
                        "that are not available on every target"
                    )
                unknown = set(entry) - {
                    "url", "http_headers", "headers", "enabled",
                }
                if unknown:
                    raise ValueError(
                        f"OpenCode MCP server '{name}' has unsupported settings: "
                        + ", ".join(sorted(unknown))
                    )
                settings = {"type": "remote", "url": url}
                headers = dict(entry.get("http_headers") or entry.get("headers") or {})
                if headers:
                    settings["headers"] = headers
            else:
                raise ValueError(f"OpenCode MCP server '{name}' needs command or url")
            converted[name] = settings
        document["mcp"] = converted
    if config.plugins:
        document["plugin"] = list(config.plugins)
    return json.dumps(document, ensure_ascii=False, sort_keys=True)


def install_opencode(target: Any, cache: str) -> str:
    """Reuse or download one native CLI binary into the harness-owned cache."""
    platform = target.text(target.login_bash("uname -sm")).strip().casefold()
    if "windows" in platform:
        system = "windows"
    elif "darwin" in platform:
        system = "darwin"
    elif "linux" in platform:
        system = "linux"
    else:
        raise RuntimeError(f"OpenCode does not publish a CLI for target platform {platform}")
    arch = "arm64" if "arm64" in platform or "aarch64" in platform else "x64"
    if all(value not in platform for value in ("x86_64", "amd64", "x64")) and arch != "arm64":
        raise RuntimeError(f"OpenCode does not publish a CLI for target architecture {platform}")
    name = f"opencode-{system}-{arch}"
    suffix = ".zip" if system != "linux" else ".tar.gz"
    executable = f"{cache}/opencode" + (".exe" if system == "windows" else "")
    cached = target.login_bash('test -x "$1"', executable, check=False)
    if cached.returncode == 0:
        return executable

    request = urllib.request.Request(
        "https://api.github.com/repos/anomalyco/opencode/releases/latest",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "test-wsl2-llm"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        release = json.load(response)
    asset = next(
        (item for item in release.get("assets", []) if item.get("name") == name + suffix),
        None,
    )
    if not asset:
        raise RuntimeError(f"Official OpenCode release has no {name + suffix} asset")
    with urllib.request.urlopen(asset["browser_download_url"], timeout=120) as response:
        archive = response.read()
    digest = asset.get("digest")
    if (
        isinstance(digest, str)
        and digest.startswith("sha256:")
        and hashlib.sha256(archive).hexdigest() != digest.removeprefix("sha256:")
    ):
        raise RuntimeError("OpenCode release asset checksum did not match")
    with tempfile.TemporaryDirectory(prefix="test-wsl2-llm-opencode-") as temporary:
        archive_path = Path(temporary) / (name + suffix)
        archive_path.write_bytes(archive)
        if suffix == ".zip":
            with zipfile.ZipFile(archive_path) as package:
                candidates = [
                    member
                    for member in package.infolist()
                    if Path(member.filename).name.casefold() == Path(executable).name.casefold()
                ]
                if len(candidates) != 1:
                    raise RuntimeError("OpenCode archive has no unique CLI binary")
                binary = package.read(candidates[0])
        else:
            with tarfile.open(archive_path, "r:gz") as package:
                candidates = [
                    member
                    for member in package.getmembers()
                    if member.isfile() and Path(member.name).name == "opencode"
                ]
                if len(candidates) != 1:
                    raise RuntimeError("OpenCode archive has no unique CLI binary")
                stream = package.extractfile(candidates[0])
                if stream is None:
                    raise RuntimeError("OpenCode archive binary is unreadable")
                binary = stream.read()
    staging = f"{executable}.{uuid.uuid4().hex}.tmp"
    writer = getattr(target, "write_file", None)
    if callable(writer):
        writer(staging, binary)
    else:
        target.bash(
            'mkdir -p -- "$(dirname -- "$1")" && cat > "$1"', staging, input_bytes=binary
        )
    if system != "windows":
        target.bash('chmod 755 -- "$1"', staging)
    target.bash(
        'if test -x "$2"; then rm -f -- "$1"; else mv -f -- "$1" "$2"; fi',
        staging,
        executable,
    )
    return executable
