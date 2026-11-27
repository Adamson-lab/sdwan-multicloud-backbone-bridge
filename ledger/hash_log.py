from dataclasses import dataclass
import hashlib
import json
from typing import Any


@dataclass(frozen=True)
class LogRecord:
    seq: int
    prev_hash: str
    kind: str
    fencing_token: int
    payload_json: str
    record_hash: str


def digest_record(seq: int, prev_hash: str, kind: str, fencing_token: int, payload_json: str) -> str:
    raw = f"{seq}|{prev_hash}|{kind}|{fencing_token}|{payload_json}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class HashChainedLog:
    def __init__(self) -> None:
        self._items: list[LogRecord] = []

    def append(self, kind: str, fencing_token: int, payload: dict[str, Any]) -> LogRecord:
        seq = len(self._items) + 1
        prev_hash = self._items[-1].record_hash if self._items else "0" * 64
        payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        record_hash = digest_record(seq, prev_hash, kind, fencing_token, payload_json)
        rec = LogRecord(
            seq=seq,
            prev_hash=prev_hash,
            kind=kind,
            fencing_token=fencing_token,
            payload_json=payload_json,
            record_hash=record_hash,
        )
        self._items.append(rec)
        return rec

    def verify(self) -> tuple[bool, str]:
        prev = "0" * 64
        for idx, item in enumerate(self._items, start=1):
            if item.seq != idx:
                return False, f"sequence gap at {idx}"
            if item.prev_hash != prev:
                return False, f"prev_hash broken at seq {item.seq}"
            expected = digest_record(item.seq, item.prev_hash, item.kind, item.fencing_token, item.payload_json)
            if expected != item.record_hash:
                return False, f"hash mismatch at seq {item.seq}"
            prev = item.record_hash
        return True, "ok"

    def items(self) -> list[LogRecord]:
        return list(self._items)
