"""Host-side credential storage and service token resolution."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_forge import config as _config_mod

CREDENTIALS_FILENAME = "credentials.json"
_SERVICE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

KNOWN_SERVICES: dict[str, tuple[str, ...]] = {
    "github": ("GITHUB_TOKEN", "GH_TOKEN"),
    "pypi": ("PYPI_TOKEN", "TWINE_PASSWORD"),
    "npm": ("NPM_TOKEN",),
}


@dataclass(frozen=True)
class CredentialRecord:
    """Stored host-side credential metadata and secret token."""

    service: str
    token: str
    username: str = ""
    host: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class CredentialStatus:
    """Non-secret summary of a service's authentication state."""

    service: str
    configured: bool
    source: str
    username: str = ""
    host: str = ""
    token_preview: str = ""


def normalize_service_name(service: str) -> str:
    """Normalize and validate a service identifier."""
    normalized = service.strip().lower()
    if not _SERVICE_NAME_RE.fullmatch(normalized):
        msg = (
            f"Invalid service name: '{service}'. "
            "Use lowercase alphanumeric characters, '.', '_', or '-'."
        )
        raise ValueError(msg)
    return normalized


def mask_token(token: str) -> str:
    """Return a masked token preview suitable for terminal or JSON display."""
    cleaned = token.strip()
    if not cleaned:
        return ""
    if len(cleaned) < 10:
        return "***"
    return f"{cleaned[:4]}...{cleaned[-4:]}"


def scrub_secret(text: str, secret: str | None) -> str:
    """Remove any occurrence of *secret* from *text*."""
    if not text or not secret:
        return text
    return text.replace(secret, "[REDACTED]")


class CredentialStore:
    """File-backed host credential store persisted with owner-only ``0600`` mode."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = (
            path if path is not None else (_config_mod.USER_CONFIG_DIR / CREDENTIALS_FILENAME)
        )

    @property
    def path(self) -> Path:
        """Return the credentials file path on disk."""
        return self._path

    def _load_raw(self) -> dict[str, dict[str, Any]]:
        if not self._path.is_file():
            return {}
        try:
            loaded = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(loaded, dict):
            return {}
        return {
            str(k): dict(v) for k, v in loaded.items() if isinstance(k, str) and isinstance(v, dict)
        }

    def _save_raw(self, data: dict[str, dict[str, Any]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with os.scandir(self._path.parent):
            pass
        os.chmod(self._path.parent, 0o700)
        fd, tmp_name = tempfile.mkstemp(prefix=".cred-", dir=str(self._path.parent))
        tmp_path = Path(tmp_name)
        try:
            os.chmod(tmp_path, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, sort_keys=True)
                handle.write("\n")
            os.replace(tmp_path, self._path)
            os.chmod(self._path, 0o600)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise

    def set(
        self,
        service: str,
        token: str,
        *,
        username: str = "",
        host: str = "",
    ) -> CredentialRecord:
        """Persist or update credentials for *service*."""
        normalized = normalize_service_name(service)
        cleaned_token = token.strip()
        if not cleaned_token:
            msg = f"Credential token for '{normalized}' must not be empty."
            raise ValueError(msg)
        updated_at = datetime.now(UTC).isoformat()
        data = self._load_raw()
        data[normalized] = {
            "token": cleaned_token,
            "username": username.strip(),
            "host": host.strip(),
            "updated_at": updated_at,
        }
        self._save_raw(data)
        return CredentialRecord(
            service=normalized,
            token=cleaned_token,
            username=username.strip(),
            host=host.strip(),
            updated_at=updated_at,
        )

    def get(self, service: str) -> CredentialRecord | None:
        """Return the stored :class:`CredentialRecord` for *service*, or ``None``."""
        normalized = normalize_service_name(service)
        entry = self._load_raw().get(normalized)
        if not entry:
            return None
        token = str(entry.get("token", "")).strip()
        if not token:
            return None
        return CredentialRecord(
            service=normalized,
            token=token,
            username=str(entry.get("username", "")),
            host=str(entry.get("host", "")),
            updated_at=str(entry.get("updated_at", "")),
        )

    def delete(self, service: str) -> bool:
        """Delete stored credentials for *service*, returning whether one existed."""
        normalized = normalize_service_name(service)
        data = self._load_raw()
        if normalized not in data:
            return False
        del data[normalized]
        self._save_raw(data)
        return True

    def list_records(self) -> list[CredentialRecord]:
        """List all stored service credentials sorted by service name."""
        records: list[CredentialRecord] = []
        for service_name, entry in sorted(self._load_raw().items()):
            token = str(entry.get("token", "")).strip()
            if not token:
                continue
            records.append(
                CredentialRecord(
                    service=service_name,
                    token=token,
                    username=str(entry.get("username", "")),
                    host=str(entry.get("host", "")),
                    updated_at=str(entry.get("updated_at", "")),
                )
            )
        return records


def resolve_service_token(
    service: str,
    *,
    env_vars: tuple[str, ...] = (),
    store: CredentialStore | None = None,
) -> tuple[str | None, str]:
    """Resolve a host credential token from env vars first, then :class:`CredentialStore`."""
    normalized = normalize_service_name(service)
    candidates = env_vars or KNOWN_SERVICES.get(normalized, ())
    for env_name in candidates:
        value = os.environ.get(env_name, "").strip()
        if value:
            return value, f"env:{env_name}"
    active_store = store if store is not None else CredentialStore()
    record = active_store.get(normalized)
    if record is not None:
        return record.token, "store"
    return None, "none"


def resolve_github_token(*, store: CredentialStore | None = None) -> tuple[str | None, str]:
    """Resolve GitHub token from ``GITHUB_TOKEN``, ``GH_TOKEN``, or host store."""
    return resolve_service_token("github", env_vars=("GITHUB_TOKEN", "GH_TOKEN"), store=store)


def resolve_auth_env_fallback(
    env_name: str,
    *,
    store: CredentialStore | None = None,
) -> str | None:
    """Resolve a host credential matching an env var name or its known service alias."""
    upper = env_name.strip().upper()
    if not upper:
        return None
    active_store = store if store is not None else CredentialStore()
    for service_name, vars_tuple in KNOWN_SERVICES.items():
        if upper in vars_tuple:
            rec = active_store.get(service_name)
            if rec is not None:
                return rec.token
    rec = active_store.get(env_name.strip().lower())
    return rec.token if rec is not None else None


def get_service_status(
    service: str,
    *,
    store: CredentialStore | None = None,
) -> CredentialStatus:
    """Return masked status information for *service* without exposing the secret."""
    normalized = normalize_service_name(service)
    active_store = store if store is not None else CredentialStore()
    token, source = resolve_service_token(normalized, store=active_store)
    record = active_store.get(normalized)
    if not token:
        return CredentialStatus(service=normalized, configured=False, source="none")
    return CredentialStatus(
        service=normalized,
        configured=True,
        source=source,
        username=record.username if record is not None and source == "store" else "",
        host=record.host if record is not None and source == "store" else "",
        token_preview=mask_token(token),
    )


def import_github_cli_token(*, store: CredentialStore | None = None) -> CredentialRecord:
    """Import an authenticated GitHub token via ``gh auth token`` into *store*."""
    try:
        proc = subprocess.run(
            ["gh", "auth", "token"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except OSError as exc:
        msg = f"GitHub CLI ('gh') is unavailable: {exc}"
        raise RuntimeError(msg) from exc
    if proc.returncode != 0 or not proc.stdout.strip():
        detail = proc.stderr.strip() or "gh auth token returned empty output"
        msg = f"Unable to import token from GitHub CLI: {detail}"
        raise RuntimeError(msg)
    active_store = store if store is not None else CredentialStore()
    return active_store.set("github", proc.stdout.strip(), host="github.com")
