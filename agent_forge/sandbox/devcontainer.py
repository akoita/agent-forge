"""Repository devcontainer and Dockerfile discovery, validation, and image build helpers."""

from __future__ import annotations

import hashlib
import json
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from docker.errors import APIError, BuildError, ImageNotFound

from agent_forge.llm.errors import SandboxStartupError

_DEVCONTAINER_CANDIDATES: tuple[tuple[str, ...], ...] = (
    (".devcontainer", "devcontainer.json"),
    (".devcontainer.json",),
)


@dataclass(frozen=True)
class DevcontainerSpec:
    """Resolved devcontainer or Dockerfile sandbox specification."""

    source_path: Path
    image: str | None = None
    dockerfile: Path | None = None
    build_context: Path | None = None
    build_args: dict[str, str] = field(default_factory=dict)
    build_target: str | None = None
    container_env: dict[str, str] = field(default_factory=dict)
    post_create_command: str | None = None


def _strip_json_comments(raw: str) -> str:
    """Strip // and /* */ comments outside JSON double-quoted strings."""
    out: list[str] = []
    idx = 0
    length = len(raw)
    in_string = False
    while idx < length:
        ch = raw[idx]
        if in_string:
            out.append(ch)
            if ch == "\\" and idx + 1 < length:
                idx += 1
                out.append(raw[idx])
            elif ch == '"':
                in_string = False
            idx += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            idx += 1
        elif ch == "/" and idx + 1 < length and raw[idx + 1] == "/":
            idx += 2
            while idx < length and raw[idx] not in "\r\n":
                idx += 1
        elif ch == "/" and idx + 1 < length and raw[idx + 1] == "*":
            end_idx = raw.find("*/", idx + 2)
            idx = length if end_idx == -1 else end_idx + 2
        else:
            out.append(ch)
            idx += 1
    return "".join(out)


def _strip_trailing_commas(raw: str) -> str:
    """Remove JSONC trailing commas before '}' or ']' outside string literals."""
    out: list[str] = []
    idx = 0
    length = len(raw)
    in_string = False
    while idx < length:
        ch = raw[idx]
        if in_string:
            out.append(ch)
            if ch == "\\" and idx + 1 < length:
                idx += 1
                out.append(raw[idx])
            elif ch == '"':
                in_string = False
            idx += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            idx += 1
        elif ch == ",":
            lookahead = idx + 1
            while lookahead < length and raw[lookahead].isspace():
                lookahead += 1
            if lookahead < length and raw[lookahead] in "}]":
                idx += 1
                continue
            out.append(ch)
            idx += 1
        else:
            out.append(ch)
            idx += 1
    return "".join(out)


def _strip_jsonc(raw: str) -> str:
    """Strip JSONC comments and trailing commas before standard ``json.loads``."""
    return _strip_trailing_commas(_strip_json_comments(raw))


def _resolve_within_repo(candidate: Path, repo_root: Path, label: str) -> Path:
    resolved = candidate.resolve()
    if not resolved.is_relative_to(repo_root):
        msg = f"{label} escapes repository root: {candidate}"
        raise ValueError(msg)
    return resolved


def _parse_post_create_command(raw: Any) -> str | None:
    if isinstance(raw, str):
        cleaned = raw.strip()
        return cleaned or None
    if isinstance(raw, list) and all(isinstance(part, str) for part in raw) and raw:
        return shlex.join(raw)
    return None


def _parse_string_dict(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    return {
        str(k): str(v)
        for k, v in raw.items()
        if isinstance(k, str) and isinstance(v, (str, int, float, bool))
    }


def _parse_build_config(
    data: dict[str, Any],
    base_dir: Path,
    repo_root: Path,
) -> tuple[Path | None, Path | None, dict[str, str], str | None]:
    raw_build = data.get("build")
    build_dict = raw_build if isinstance(raw_build, dict) else {}
    raw_dockerfile = build_dict.get("dockerfile") or data.get("dockerFile")
    if not isinstance(raw_dockerfile, str) or not raw_dockerfile.strip():
        return None, None, {}, None

    dockerfile = _resolve_within_repo(
        base_dir / raw_dockerfile.strip(),
        repo_root,
        "Devcontainer Dockerfile",
    )
    if not dockerfile.is_file():
        msg = f"Devcontainer Dockerfile not found: {dockerfile}"
        raise ValueError(msg)

    raw_context = build_dict.get("context") or data.get("context")
    context_candidate = (
        base_dir / raw_context.strip()
        if isinstance(raw_context, str) and raw_context.strip()
        else base_dir
    )
    build_context = _resolve_within_repo(context_candidate, repo_root, "Devcontainer build context")
    if not build_context.is_dir():
        msg = f"Devcontainer build context directory not found: {build_context}"
        raise ValueError(msg)

    raw_target = build_dict.get("target")
    build_target = (
        raw_target.strip() if isinstance(raw_target, str) and raw_target.strip() else None
    )
    build_args = _parse_string_dict(build_dict.get("args"))
    return dockerfile, build_context, build_args, build_target


def discover_devcontainer(repo_path: str | Path) -> DevcontainerSpec | None:
    """Discover and parse ``.devcontainer/devcontainer.json`` or ``.devcontainer.json``."""
    repo_root = Path(repo_path).resolve()
    if not repo_root.is_dir():
        return None

    config_path: Path | None = None
    for parts in _DEVCONTAINER_CANDIDATES:
        candidate = repo_root.joinpath(*parts)
        if candidate.is_file():
            config_path = candidate
            break
    if config_path is None:
        return None

    try:
        parsed = json.loads(_strip_jsonc(config_path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as exc:
        msg = f"Invalid devcontainer configuration at {config_path}: {exc}"
        raise ValueError(msg) from exc

    if not isinstance(parsed, dict):
        msg = f"Devcontainer configuration must be a JSON object: {config_path}"
        raise ValueError(msg)

    raw_image = parsed.get("image")
    image = raw_image.strip() if isinstance(raw_image, str) and raw_image.strip() else None
    dockerfile, build_context, build_args, build_target = _parse_build_config(
        parsed,
        config_path.parent,
        repo_root,
    )
    if image is None and dockerfile is None:
        return None

    return DevcontainerSpec(
        source_path=config_path,
        image=image,
        dockerfile=dockerfile,
        build_context=build_context,
        build_args=build_args,
        build_target=build_target,
        container_env=_parse_string_dict(parsed.get("containerEnv")),
        post_create_command=_parse_post_create_command(parsed.get("postCreateCommand")),
    )


def resolve_dockerfile_spec(repo_path: str | Path, dockerfile: str) -> DevcontainerSpec:
    """Resolve an explicit repository-relative Dockerfile path into a :class:`DevcontainerSpec`."""
    cleaned = dockerfile.strip()
    if not cleaned:
        msg = "Dockerfile path cannot be empty"
        raise ValueError(msg)
    repo_root = Path(repo_path).resolve()
    resolved = _resolve_within_repo(repo_root / cleaned, repo_root, "Sandbox Dockerfile")
    if not resolved.is_file():
        msg = f"Sandbox Dockerfile not found: {resolved}"
        raise ValueError(msg)
    return DevcontainerSpec(
        source_path=resolved,
        dockerfile=resolved,
        build_context=repo_root,
    )


def compute_dockerfile_image_tag(spec: DevcontainerSpec) -> str:
    """Compute a deterministic Docker image tag from Dockerfile content and build inputs."""
    if spec.dockerfile is None:
        msg = "Cannot compute Dockerfile image tag without a Dockerfile path"
        raise ValueError(msg)

    hasher = hashlib.sha256()
    if spec.build_context is not None and spec.dockerfile.is_relative_to(spec.build_context):
        rel_name = spec.dockerfile.relative_to(spec.build_context).as_posix()
    else:
        rel_name = spec.dockerfile.name
    hasher.update(rel_name.encode())
    hasher.update(b"\0")
    hasher.update(spec.dockerfile.read_bytes())
    for key, value in sorted(spec.build_args.items()):
        hasher.update(f"\0{key}={value}".encode())
    if spec.build_target:
        hasher.update(f"\0target={spec.build_target}".encode())
    return f"agent-forge-repo:{hasher.hexdigest()[:12]}"


def ensure_dockerfile_image(client: Any, spec: DevcontainerSpec) -> str:
    """Return a local Docker image tag for *spec*, building it if not yet cached."""
    if spec.dockerfile is None or spec.build_context is None:
        msg = "DevcontainerSpec requires both dockerfile and build_context to build an image"
        raise SandboxStartupError(msg)

    tag = compute_dockerfile_image_tag(spec)
    try:
        client.images.get(tag)
        return tag
    except ImageNotFound:
        pass
    except APIError as exc:
        msg = f"Failed to inspect Docker image '{tag}': {exc}"
        raise SandboxStartupError(msg) from exc

    try:
        client.images.build(
            path=str(spec.build_context),
            dockerfile=str(spec.dockerfile),
            tag=tag,
            buildargs=spec.build_args or None,
            target=spec.build_target,
            rm=True,
        )
    except (BuildError, APIError, OSError) as exc:
        msg = f"Failed to build sandbox Dockerfile '{spec.dockerfile}': {exc}"
        raise SandboxStartupError(msg) from exc
    return tag
