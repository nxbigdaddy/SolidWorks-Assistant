from __future__ import annotations

import base64
import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..domain import ConfigurationError


SETTINGS_VERSION = 1


@dataclass(slots=True)
class ModelSettings:
    api_base: str = ""
    model: str = ""
    api_key: str = ""
    timeout_seconds: int = 90
    credential_unreadable: bool = False

    @property
    def configured(self) -> bool:
        return bool(self.api_base and self.model)

    def validate(self) -> None:
        from urllib.parse import urlsplit

        if bool(self.api_base) != bool(self.model):
            raise ConfigurationError("API 地址和模型名称必须同时填写或同时留空。")
        if not self.configured:
            return
        parsed = urlsplit(self.api_base)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ConfigurationError("API 地址必须是完整的 http/https URL。")
        if parsed.username or parsed.password or parsed.fragment:
            raise ConfigurationError("API 地址不能包含凭据或 URL 片段。")
        if not 1 <= int(self.timeout_seconds) <= 300:
            raise ConfigurationError("模型超时必须位于 1 到 300 秒之间。")


@dataclass(slots=True)
class AppSettings:
    model: ModelSettings = field(default_factory=ModelSettings)
    library_page_size: int = 200
    log_retention_days: int = 30
    agent_enabled: bool = True
    agent_max_model_requests: int = 6
    agent_backend: str = "auto"
    pi_max_repair_rounds: int = 6
    auto_repair_failures: bool = True
    auto_resume_after_repair: bool = True
    document_manager_key: str = ""
    document_manager_credential_unreadable: bool = False

    def validate(self) -> None:
        self.model.validate()
        if not 1 <= int(self.library_page_size) <= 1000:
            raise ConfigurationError("模型库分页大小必须位于 1 到 1000 之间。")
        if not 1 <= int(self.log_retention_days) <= 3650:
            raise ConfigurationError("日志保留天数必须位于 1 到 3650 之间。")
        if not 2 <= int(self.agent_max_model_requests) <= 8:
            raise ConfigurationError("全流程 Agent 单次模型请求上限必须位于 2 到 8 次之间。")
        if self.agent_backend not in {"auto", "pi", "pydantic"}:
            raise ConfigurationError("Agent 后端必须是 auto、pi 或 pydantic。")
        if not 2 <= int(self.pi_max_repair_rounds) <= 12:
            raise ConfigurationError("Pi 多轮修正上限必须位于 2 到 12 次之间。")


def _protect(value: str) -> str:
    if not value:
        return ""
    try:
        import win32crypt

        blob = win32crypt.CryptProtectData(
            value.encode("utf-8"), "sw_assistant", None, None, None, 0
        )
    except Exception as exc:
        raise ConfigurationError("无法使用 Windows DPAPI 加密 API Key。") from exc
    return "dpapi:" + base64.b64encode(blob).decode("ascii")


def _unprotect(value: str) -> str:
    if not value:
        return ""
    if not value.startswith("dpapi:"):
        raise ConfigurationError("保存的 API Key 格式不受支持。")
    try:
        import win32crypt

        blob = base64.b64decode(value[6:], validate=True)
        return win32crypt.CryptUnprotectData(blob, None, None, None, 0)[1].decode("utf-8")
    except Exception as exc:
        raise ConfigurationError("保存的 API Key 无法由当前 Windows 用户解密。") from exc


class SettingsStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.warning = ""

    def load(self) -> AppSettings:
        self.warning = ""
        if not self.path.is_file():
            return AppSettings()
        try:
            payload = json.loads(self.path.read_text("utf-8"))
            if int(payload.get("version", 0)) != SETTINGS_VERSION:
                raise ValueError("设置版本不受支持")
            model_data = dict(payload.get("model") or {})
            protected = str(model_data.pop("api_key_protected", ""))
            unreadable = False
            warnings: list[str] = []
            try:
                api_key = _unprotect(protected)
            except Exception as exc:
                api_key = ""
                unreadable = bool(protected)
                warnings.append(str(exc) + " 请重新输入并保存模型 API Key。")
            solidworks_data = dict(payload.get("solidworks") or {})
            protected_document_manager = str(
                solidworks_data.get("document_manager_key_protected") or ""
            )
            document_manager_unreadable = False
            try:
                document_manager_key = _unprotect(protected_document_manager)
            except Exception as exc:
                document_manager_key = ""
                document_manager_unreadable = bool(protected_document_manager)
                warnings.append(
                    str(exc) + " 请重新输入并保存 Document Manager 许可证密钥。"
                )
            settings = AppSettings(
                model=ModelSettings(
                    api_base=str(model_data.get("api_base") or "").strip(),
                    model=str(model_data.get("model") or "").strip(),
                    api_key=api_key,
                    timeout_seconds=int(model_data.get("timeout_seconds") or 90),
                    credential_unreadable=unreadable,
                ),
                library_page_size=int(payload.get("library_page_size") or 200),
                log_retention_days=int(payload.get("log_retention_days") or 30),
                agent_enabled=bool(payload.get("agent_enabled", True)),
                agent_max_model_requests=int(
                    payload.get("agent_max_model_requests") or 6
                ),
                agent_backend=str(payload.get("agent_backend") or "auto"),
                pi_max_repair_rounds=int(
                    payload.get("pi_max_repair_rounds") or 6
                ),
                auto_repair_failures=bool(payload.get("auto_repair_failures", True)),
                auto_resume_after_repair=bool(
                    payload.get("auto_resume_after_repair", True)
                ),
                document_manager_key=document_manager_key,
                document_manager_credential_unreadable=document_manager_unreadable,
            )
            settings.validate()
            self.warning = " ".join(warnings)
            return settings
        except Exception as exc:
            self.warning = f"设置文件无法读取，已使用安全默认值：{exc}"
            return AppSettings()

    def save(self, settings: AppSettings) -> None:
        settings.validate()
        if settings.model.credential_unreadable and not settings.model.api_key:
            raise ConfigurationError("请重新输入 API Key，不能继续保存不可解密的旧凭据。")
        if (
            settings.document_manager_credential_unreadable
            and not settings.document_manager_key
        ):
            raise ConfigurationError(
                "请重新输入 Document Manager 许可证密钥，不能保存不可解密的旧凭据。"
            )
        model = asdict(settings.model)
        model.pop("credential_unreadable", None)
        api_key = str(model.pop("api_key", ""))
        model["api_key_protected"] = _protect(api_key)
        payload = {
            "version": SETTINGS_VERSION,
            "model": model,
            "solidworks": {
                "document_manager_key_protected": _protect(
                    settings.document_manager_key
                )
            },
            "library_page_size": int(settings.library_page_size),
            "log_retention_days": int(settings.log_retention_days),
            "agent_enabled": bool(settings.agent_enabled),
            "agent_max_model_requests": int(settings.agent_max_model_requests),
            "agent_backend": settings.agent_backend,
            "pi_max_repair_rounds": int(settings.pi_max_repair_rounds),
            "auto_repair_failures": bool(settings.auto_repair_failures),
            "auto_resume_after_repair": bool(settings.auto_resume_after_repair),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(
            prefix=self.path.name + ".", suffix=".tmp", dir=self.path.parent
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
        self.warning = ""
