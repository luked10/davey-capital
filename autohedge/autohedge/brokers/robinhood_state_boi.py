from __future__ import annotations

import json
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class RobinhoodStateBoi:
    username: str | None = None
    session_pickle_path: str | None = None
    state_path: str | None = None
    last_login_at: str | None = None
    stock_account_id: str | None = None
    crypto_account_id: str | None = None
    asset_classes: list[str] = field(default_factory=lambda: ['stock', 'crypto'])
    metadata: dict[str, Any] = field(default_factory=dict)


class RobinhoodStateStoreBoi:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self) -> RobinhoodStateBoi:
        if not self.path.exists():
            return RobinhoodStateBoi(state_path=str(self.path))
        payload = json.loads(self.path.read_text())
        return RobinhoodStateBoi(
            username=payload.get('username'),
            session_pickle_path=payload.get('session_pickle_path'),
            state_path=payload.get('state_path', str(self.path)),
            last_login_at=payload.get('last_login_at'),
            stock_account_id=payload.get('stock_account_id'),
            crypto_account_id=payload.get('crypto_account_id'),
            asset_classes=payload.get('asset_classes', ['stock', 'crypto']),
            metadata=payload.get('metadata', {}),
        )

    def save(self, state: RobinhoodStateBoi) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = asdict(state)
        payload['state_path'] = str(self.path)
        self.path.write_text(json.dumps(payload, indent=2, sort_keys=True))
