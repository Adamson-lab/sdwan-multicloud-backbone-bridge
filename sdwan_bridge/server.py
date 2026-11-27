from datetime import datetime, timezone
import json
import os
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
import httpx

from ledger.hash_log import HashChainedLog
from sdwan_bridge.steering import (
    BranchLeaseConflict,
    BranchLeaseRegistry,
    SdwanBackboneConnector,
    StaleSteeringToken,
    verify_steering_payload,
)

app = FastAPI(title="sdwan-multicloud-backbone-bridge", version="0.1.0")

LOG = HashChainedLog()
REGISTRY = BranchLeaseRegistry(log=LOG, ttl_sec=25.0)


def _eval_opa(opa_url: str, opa_input: dict[str, Any]) -> tuple[bool, str]:
    base = opa_url.rstrip("/")
    with httpx.Client(timeout=3.0) as client:
        r1 = client.post(f"{base}/v1/data/sdwan/backbone", json={"input": opa_input})
        r1.raise_for_status()
        d1 = r1.json().get("result") or {}
        if not d1.get("allow", False):
            return False, d1.get("reason", "denied by backbone steering policy")

        r2 = client.post(f"{base}/v1/data/sdwan/window", json={"input": opa_input})
        r2.raise_for_status()
        d2 = r2.json().get("result") or {}
        if not d2.get("allow", False):
            return False, d2.get("reason", "denied by change window policy")

        return True, d2.get("reason", "allowed")


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    ok, msg = LOG.verify()
    return {
        "status": "ok" if ok else "degraded",
        "log_status": msg,
        "utc_time": datetime.now(timezone.utc).isoformat(),
        "state": REGISTRY.snapshot(),
    }


@app.post("/v1/sdwan/steer")
async def steer_branch_traffic(
    request: Request,
    x_signature_256: str | None = Header(default=None, alias="X-Signature-256"),
    x_timestamp: str | None = Header(default=None, alias="X-Timestamp"),
) -> dict[str, Any]:
    raw_body = await request.body()
    secret = os.getenv("WEBHOOK_HMAC_SECRET", "local-dev-sdwan-backbone-secret")
    valid, reason = verify_steering_payload(secret, x_signature_256, x_timestamp, raw_body)
    if not valid:
        raise HTTPException(status_code=401, detail={"error": reason})

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=400, detail={"error": "malformed json payload"})

    branch_id = str(payload.get("branch_id", "branch-lagos-01"))
    cloud_backbone = str(payload.get("cloud_backbone", "aws-tgw"))
    sla_class = str(payload.get("sla_class", "low-latency-voice"))
    bgp_asn = int(payload.get("bgp_asn", 65100))
    holder_id = str(payload.get("holder_id", "sdwan-edge-1"))
    role = str(payload.get("actor_role", "sdwan-ops"))
    grace_lock = payload.get("grace_lock") or {}
    grace_mins = int(grace_lock.get("extra_minutes", 0)) if grace_lock.get("active") else 0
    now_utc = datetime.now(timezone.utc)

    opa_url = os.getenv("OPA_URL", "http://localhost:8181")
    try:
        allowed, policy_reason = _eval_opa(
            opa_url,
            {
                "branch_id": branch_id,
                "cloud_backbone": cloud_backbone,
                "sla_class": sla_class,
                "bgp_asn": bgp_asn,
                "actor_role": role,
                "utc_hour": now_utc.hour,
                "grace_lock": grace_lock,
            },
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail={"error": f"opa check failed: {exc}"})

    if not allowed:
        raise HTTPException(status_code=403, detail={"error": policy_reason})

    branch_key = f"{branch_id}:{cloud_backbone}"
    try:
        lease = REGISTRY.acquire(
            branch_key=branch_key,
            holder_id=holder_id,
            now_utc=now_utc.timestamp(),
            grace_extension_sec=grace_mins * 60.0,
        )
    except BranchLeaseConflict as exc:
        raise HTTPException(status_code=409, detail={"error": str(exc)})

    connector = SdwanBackboneConnector(
        registry=REGISTRY,
        region=os.getenv("AWS_REGION", "us-east-1"),
        endpoint_url=os.getenv("AWS_ENDPOINT_URL", "http://localhost:4566"),
        bucket=os.getenv("SDWAN_AUDIT_BUCKET", "sdwan-multicloud-backbone-audit"),
    )
    try:
        res = connector.steer_branch(
            branch_id=branch_id,
            cloud_backbone=cloud_backbone,
            sla_class=sla_class,
            bgp_asn=bgp_asn,
            fencing_token=lease.fencing_token,
            now_utc=now_utc.timestamp(),
        )
    except StaleSteeringToken as exc:
        raise HTTPException(status_code=409, detail={"error": str(exc)})
    except Exception as exc:
        raise HTTPException(status_code=502, detail={"error": f"steering failed: {exc}"})

    chain_ok, _ = LOG.verify()
    return {
        "status": "steered",
        "policy_reason": policy_reason,
        "fencing_token": lease.fencing_token,
        "hash_chain_valid": chain_ok,
        "steering": {
            "branch_id": res.branch_id,
            "cloud_backbone": res.cloud_backbone,
            "sla_class": res.sla_class,
            "bgp_asn": res.bgp_asn,
            "s3_bucket": res.s3_bucket,
            "s3_key": res.s3_key,
            "log_hash": res.log_hash,
        },
    }
