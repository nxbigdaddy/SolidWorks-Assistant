from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class AppPaths:
    data: Path
    logs: Path
    settings: Path
    library_db: Path
    history_db: Path

    @classmethod
    def default(cls) -> "AppPaths":
        override = os.getenv("SW_ASSISTANT_DATA_DIR", "").strip()
        base = Path(override).expanduser() if override else Path(
            os.getenv("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
        ) / "SolidWorksAIAssistantV5"
        base = base.resolve()
        return cls(
            data=base,
            logs=base / "logs",
            settings=base / "settings.json",
            library_db=base / "library.db",
            history_db=base / "history.db",
        )

    def ensure(self) -> None:
        self.data.mkdir(parents=True, exist_ok=True)
        self.logs.mkdir(parents=True, exist_ok=True)
        if not os.getenv("SW_ASSISTANT_DATA_DIR", "").strip() and self.data == self.default().data:
            from .v5_migration import import_previous_data

            import_previous_data(self.data.parent / "SolidWorksAIAssistant", self.data)

    @property
    def event_bridge(self) -> Path:
        override = os.getenv("SW_ASSISTANT_EVENT_BRIDGE_DIR", "").strip()
        if override:
            return Path(override).expanduser().resolve()
        if self.data.name == "SolidWorksAIAssistantV5":
            # The registered Add-in is a shared read-only event source, not user settings.
            return self.data.parent / "SolidWorksAIAssistant" / "event-bridge"
        return self.data / "event-bridge"
