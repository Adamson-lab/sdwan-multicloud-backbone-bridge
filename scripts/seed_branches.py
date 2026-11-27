import json
import os
from pathlib import Path
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from ledger.hash_log import HashChainedLog
from sdwan_bridge.steering import BranchLeaseRegistry, SdwanBackboneConnector

INITIAL_BRANCHES = [
    ("branch-lagos-01", "aws-tgw", "low-latency-voice", 65100, "seed-node-1"),
    ("branch-london-02", "azure-vwan", "standard-branch", 65101, "seed-node-1"),
    ("branch-frankfurt-03", "gcp-ncc", "bulk-replication", 65102, "seed-node-1"),
]


def seed_initial_branches(
    connector: SdwanBackboneConnector,
    registry: BranchLeaseRegistry,
    now_utc: float = 1764267000.0,
) -> list[dict]:
    out = []
    for idx, (branch_id, backbone, sla, asn, holder) in enumerate(INITIAL_BRANCHES, start=1):
        key = f"{branch_id}:{backbone}"
        lease = registry.acquire(branch_key=key, holder_id=holder, now_utc=now_utc + idx)
        res = connector.steer_branch(
            branch_id=branch_id,
            cloud_backbone=backbone,
            sla_class=sla,
            bgp_asn=asn,
            fencing_token=lease.fencing_token,
            now_utc=now_utc + idx,
        )
        out.append({
            "branch_id": res.branch_id,
            "cloud_backbone": res.cloud_backbone,
            "s3_key": res.s3_key,
            "log_hash": res.log_hash,
        })
    return out


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    env_file = root / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s and not s.startswith("#") and "=" in s:
                k, v = s.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

    log = HashChainedLog()
    reg = BranchLeaseRegistry(log=log, ttl_sec=60.0)
    conn = SdwanBackboneConnector(
        registry=reg,
        region=os.getenv("AWS_REGION", "us-east-1"),
        endpoint_url=os.getenv("AWS_ENDPOINT_URL", "http://localhost:4566"),
        bucket=os.getenv("SDWAN_AUDIT_BUCKET", "sdwan-multicloud-backbone-audit"),
    )
    seeded = seed_initial_branches(conn, reg)
    print(json.dumps({"seeded": len(seeded), "chain_ok": log.verify()[0], "branches": seeded}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
