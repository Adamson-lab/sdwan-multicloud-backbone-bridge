from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import json
import logging
import threading
import time
from typing import Any

from ledger.hash_log import HashChainedLog

logger = logging.getLogger("sdwan_bridge.steering")


class BranchLeaseConflict(Exception):
    pass


class StaleSteeringToken(Exception):
    pass


@dataclass
class BranchLease:
    branch_key: str
    holder_id: str
    fencing_token: int
    expires_at_utc: float


@dataclass
class SteeringResult:
    branch_id: str
    cloud_backbone: str
    sla_class: str
    bgp_asn: int
    fencing_token: int
    s3_bucket: str
    s3_key: str
    log_hash: str


def sign_steering_payload(secret: str, ts_unix: int, body: bytes) -> str:
    msg = f"{ts_unix}.".encode("utf-8") + body
    sig = hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    return f"sha256={sig}"


def verify_steering_payload(
    secret: str,
    signature: str | None,
    timestamp: str | None,
    body: bytes,
    max_skew_sec: int = 300,
    now_unix: int | None = None,
) -> tuple[bool, str]:
    if not signature or not timestamp:
        return False, "missing X-Signature-256 or X-Timestamp header"
    try:
        ts = int(timestamp.strip())
    except ValueError:
        return False, "invalid X-Timestamp header"

    # Evaluate in UTC epoch seconds so Daylight Saving Time changes never shift replay windows.
    now = int(time.time()) if now_unix is None else now_unix
    if abs(now - ts) > max_skew_sec:
        return False, "timestamp outside replay window"

    expected = sign_steering_payload(secret, ts, body)
    if not hmac.compare_digest(expected, signature.strip()):
        return False, "hmac signature mismatch"
    return True, "ok"


def _retryable_err(exc: Any) -> bool:
    resp = getattr(exc, "response", {}) or {}
    code = resp.get("Error", {}).get("Code", "")
    status = resp.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
    return code in {
        "Throttling",
        "ThrottlingException",
        "RequestLimitExceeded",
        "TooManyRequestsException",
    } or status in {429, 502, 503, 504}


def _call_aws_with_backoff(op: str, fn: Any, exc_cls: Any, attempts: int = 4) -> Any:
    delay = 0.1
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except exc_cls as exc:
            if not _retryable_err(exc) or attempt == attempts:
                raise
            # Cap per-retry sleep at 2.0s so EC2/S3 throttling does not exceed caller deadlines.
            pause = min(delay, 2.0)
            logger.warning("retrying aws op=%s attempt=%d pause_s=%.2f err=%s", op, attempt, pause, exc)
            time.sleep(pause)
            delay *= 2.0


class BranchLeaseRegistry:
    # Monotonic fencing tokens stop a stalled edge controller from applying stale backbone routes after takeover.
    def __init__(self, log: HashChainedLog | None = None, ttl_sec: float = 25.0) -> None:
        self._lock = threading.Lock()
        self._leases: dict[str, BranchLease] = {}
        self._seq: int = 0
        self._log = log or HashChainedLog()
        self._ttl_sec = ttl_sec

    def acquire(
        self,
        branch_key: str,
        holder_id: str,
        now_utc: float | None = None,
        grace_extension_sec: float = 0.0,
    ) -> BranchLease:
        now = time.time() if now_utc is None else now_utc
        clamped_grace = min(max(grace_extension_sec, 0.0), 900.0)
        with self._lock:
            current = self._leases.get(branch_key)
            if current is not None and current.expires_at_utc > now and current.holder_id != holder_id:
                raise BranchLeaseConflict(f"branch {branch_key} actively leased by {current.holder_id}")

            self._seq += 1
            lease = BranchLease(
                branch_key=branch_key,
                holder_id=holder_id,
                fencing_token=self._seq,
                expires_at_utc=now + self._ttl_sec + clamped_grace,
            )
            self._leases[branch_key] = lease
            self._log.append(
                kind="branch_lease_acquired",
                fencing_token=lease.fencing_token,
                payload={
                    "branch_key": branch_key,
                    "holder_id": holder_id,
                    "expires_at_utc": lease.expires_at_utc,
                    "grace_sec": clamped_grace,
                    "recorded_at_utc": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
                },
            )
            return lease

    def check_token(self, branch_key: str, fencing_token: int, now_utc: float | None = None) -> bool:
        now = time.time() if now_utc is None else now_utc
        with self._lock:
            lease = self._leases.get(branch_key)
            if lease is None:
                return False
            if lease.expires_at_utc <= now:
                return False
            return fencing_token == lease.fencing_token

    def log(self) -> HashChainedLog:
        return self._log

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "leases": {
                    k: {
                        "holder_id": v.holder_id,
                        "fencing_token": v.fencing_token,
                        "expires_at_utc": v.expires_at_utc,
                    }
                    for k, v in self._leases.items()
                },
                "log_length": len(self._log.items()),
            }


class SdwanBackboneConnector:
    def __init__(
        self,
        registry: BranchLeaseRegistry,
        region: str = "us-east-1",
        endpoint_url: str | None = "http://localhost:4566",
        bucket: str = "sdwan-multicloud-backbone-audit",
        ec2_client: Any | None = None,
        s3_client: Any | None = None,
        client_error_cls: Any | None = None,
    ) -> None:
        self.registry = registry
        self.region = region
        self.bucket = bucket

        if client_error_cls is not None:
            self._client_error = client_error_cls
            boto3_mod = None
        else:
            try:
                import boto3
                from botocore.exceptions import ClientError

                boto3_mod = boto3
                self._client_error = ClientError
            except ImportError:
                boto3_mod = None

                class _FallbackError(Exception):
                    response: dict[str, Any] = {}

                self._client_error = _FallbackError

        client_kwargs: dict[str, Any] = {"region_name": region}
        if endpoint_url:
            client_kwargs["endpoint_url"] = endpoint_url
            client_kwargs["aws_access_key_id"] = "test"
            client_kwargs["aws_secret_access_key"] = "test"

        if ec2_client is not None:
            self.ec2 = ec2_client
        else:
            if boto3_mod is None:
                raise RuntimeError("boto3 is required when ec2_client is not supplied")
            self.ec2 = boto3_mod.client("ec2", **client_kwargs)

        if s3_client is not None:
            self.s3 = s3_client
        else:
            if boto3_mod is None:
                raise RuntimeError("boto3 is required when s3_client is not supplied")
            self.s3 = boto3_mod.client("s3", **client_kwargs)

    def _ensure_bucket(self) -> None:
        try:
            self.s3.head_bucket(Bucket=self.bucket)
        except self._client_error:
            try:
                self.s3.create_bucket(Bucket=self.bucket)
            except self._client_error as exc:
                code = (getattr(exc, "response", {}) or {}).get("Error", {}).get("Code", "")
                if code not in {"BucketAlreadyOwnedByYou", "BucketAlreadyExists"}:
                    raise

    def steer_branch(
        self,
        branch_id: str,
        cloud_backbone: str,
        sla_class: str,
        bgp_asn: int,
        fencing_token: int,
        now_utc: float | None = None,
    ) -> SteeringResult:
        branch_key = f"{branch_id}:{cloud_backbone}"
        if not self.registry.check_token(branch_key=branch_key, fencing_token=fencing_token, now_utc=now_utc):
            raise StaleSteeringToken(f"fencing token {fencing_token} rejected for {branch_key}")

        _call_aws_with_backoff(
            "ec2:describe_transit_gateways",
            lambda: self.ec2.describe_transit_gateways(),
            self._client_error,
        )
        self._ensure_bucket()

        ts = time.time() if now_utc is None else now_utc
        dt_utc = datetime.fromtimestamp(ts, tz=timezone.utc)
        rec = self.registry.log().append(
            kind="branch_backbone_steered",
            fencing_token=fencing_token,
            payload={
                "branch_id": branch_id,
                "cloud_backbone": cloud_backbone,
                "sla_class": sla_class,
                "bgp_asn": bgp_asn,
                "steered_at_utc": dt_utc.isoformat(),
            },
        )
        s3_key = f"steering/{dt_utc.strftime('%Y-%m-%d')}/{branch_id}-{cloud_backbone}-tok{fencing_token}.json"
        doc = {
            "branch_id": branch_id,
            "cloud_backbone": cloud_backbone,
            "sla_class": sla_class,
            "bgp_asn": bgp_asn,
            "fencing_token": fencing_token,
            "log_hash": rec.record_hash,
        }
        _call_aws_with_backoff(
            "s3:put_object",
            lambda: self.s3.put_object(
                Bucket=self.bucket,
                Key=s3_key,
                Body=json.dumps(doc, indent=2).encode("utf-8"),
                ContentType="application/json",
            ),
            self._client_error,
        )
        return SteeringResult(
            branch_id=branch_id,
            cloud_backbone=cloud_backbone,
            sla_class=sla_class,
            bgp_asn=bgp_asn,
            fencing_token=fencing_token,
            s3_bucket=self.bucket,
            s3_key=s3_key,
            log_hash=rec.record_hash,
        )
