from __future__ import annotations

import os
from pathlib import Path

_SQLITE_SIDECAR_SUFFIXES = ("", "-wal", "-shm", "-journal")


def secure_sqlite_artifacts(db_path: str) -> None:
    """SQLite DB 파일과 사이드카(-wal/-shm/-journal)의 권한을 제한한다.

    DB 부모 디렉토리는 0o700, DB 파일들은 0o600으로 설정한다.
    실패해도 조용히 무시한다(권한 설정은 best-effort).
    """
    p = Path(db_path).expanduser()
    parent = p.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        return

    try:
        os.chmod(parent, 0o700)
    except Exception:
        pass

    for suffix in _SQLITE_SIDECAR_SUFFIXES:
        candidate = Path(f"{p}{suffix}")
        try:
            if candidate.exists():
                os.chmod(candidate, 0o600)
        except Exception:
            pass
