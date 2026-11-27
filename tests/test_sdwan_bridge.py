import os
import sys
import time
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from ledger.hash_log import HashChainedLog
from scripts.preflight import run_check
from scripts.seed_branches import seed_initial_branches
from sdwan_bridge.steering import (
    BranchLeaseConflict,
    BranchLeaseRegistry,
    SdwanBackboneConnector,
    StaleSteeringToken,
    sign_steering_payload,
    verify_steering_payload,
)


class TestSdwanBackboneBridge(unittest.TestCase):
    def setUp(self) -> None:
        self.log = HashChainedLog()
        self.reg = BranchLeaseRegistry(log=self.log, ttl_sec=25.0)
        self.fake_ec2 = MagicMock()
        self.fake_ec2.describe_transit_gateways.return_value = {"TransitGateways": []}
        self.fake_s3 = MagicMock()
        self.conn = SdwanBackboneConnector(
            registry=self.reg,
            region="us-east-1",
            endpoint_url=None,
            bucket="test-sdwan-bucket",
            ec2_client=self.fake_ec2,
            s3_client=self.fake_s3,
        )

    def test_steer_branch_and_verify_log(self) -> None:
        t0 = 1764267000.0
        lease = self.reg.acquire("branch-lagos-01:aws-tgw", "sdwan-edge-1", now_utc=t0, grace_extension_sec=600.0)
        res = self.conn.steer_branch(
            branch_id="branch-lagos-01",
            cloud_backbone="aws-tgw",
            sla_class="low-latency-voice",
            bgp_asn=65100,
            fencing_token=lease.fencing_token,
            now_utc=t0 + 1,
        )
        self.assertEqual(res.fencing_token, 1)
        ok, msg = self.log.verify()
        self.assertTrue(ok, msg)
        self.fake_s3.put_object.assert_called_once()

    def test_seed_and_lease_conflict(self) -> None:
        seeded = seed_initial_branches(self.conn, self.reg, now_utc=1764267000.0)
        self.assertEqual(len(seeded), 3)
        ok, _ = self.log.verify()
        self.assertTrue(ok)

        with self.assertRaises(BranchLeaseConflict):
            self.reg.acquire("branch-lagos-01:aws-tgw", "sdwan-edge-2", now_utc=1764267002.0)

    def test_chaos_stale_token_rejected_after_failover(self) -> None:
        t0 = 1764267000.0
        stale = self.reg.acquire("branch-lagos-01:aws-tgw", "sdwan-edge-1", now_utc=t0)
        # Edge-1 stalls past TTL and edge-2 acquires a higher fencing token after expiry.
        fresh = self.reg.acquire("branch-lagos-01:aws-tgw", "sdwan-edge-2", now_utc=t0 + 30)
        self.assertGreater(fresh.fencing_token, stale.fencing_token)

        with self.assertRaises(StaleSteeringToken):
            self.conn.steer_branch(
                branch_id="branch-lagos-01",
                cloud_backbone="aws-tgw",
                sla_class="low-latency-voice",
                bgp_asn=65100,
                fencing_token=stale.fencing_token,
                now_utc=t0 + 31,
            )

    def test_hmac_and_preflight_no_live(self) -> None:
        secret = "sdwan-secret"
        body = b'{"branch_id":"branch-lagos-01","cloud_backbone":"aws-tgw"}'
        now = 1764267000
        sig = sign_steering_payload(secret, now, body)
        ok, _ = verify_steering_payload(secret, sig, str(now), body, now_unix=now)
        self.assertTrue(ok)
        self.assertEqual(run_check(live=False), 0)

    def test_bench_lease_and_log(self) -> None:
        log = HashChainedLog()
        reg = BranchLeaseRegistry(log=log, ttl_sec=30.0)
        base = 1764267000.0
        iters = 2000
        t0 = time.perf_counter()
        for i in range(iters):
            key = f"branch-{i % 8}:aws-tgw"
            lease = reg.acquire(key, f"e-{i % 3}", now_utc=base + (i * 35.0))
            self.assertTrue(reg.check_token(key, lease.fencing_token, now_utc=base + (i * 35.0)))
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        ok, _ = log.verify()
        self.assertTrue(ok)
        print(f"BENCH lease+check+log ({iters} ops): {elapsed_ms:.2f} ms ({(elapsed_ms / iters) * 1000:.2f} us/op)")


if __name__ == "__main__":
    unittest.main()
