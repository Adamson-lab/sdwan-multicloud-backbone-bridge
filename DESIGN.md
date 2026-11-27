# Design Notes: SD-WAN Integration with Multicloud Backbones

This service separates branch lease tracking and SHA-256 hash-chained logging (`sdwan_bridge/steering.py`, `sdwan_bridge/server.py`, `ledger/hash_log.py`) from cloud backbone steering checks and S3 recording (`SdwanBackboneConnector`) with Open Policy Agent (OPA) policy evaluation.

## Trust-Boundary and Network Enclave Topology

```mermaid
flowchart TB
    subgraph BranchZone["SD-WAN Branch Edge"]
        Branch["SD-WAN Controller"]
    end

    subgraph ControlZone["Control Plane"]
        API["FastAPI Server :8080"]
        Registry["BranchLeaseRegistry"]
        Log["HashChainedLog"]
        API --> Registry
        Registry --> Log
    end

    subgraph PolicyAndCloud["Policy and Cloud Services"]
        OPA["OPA :8181"]
        AWS["LocalStack EC2, S3 & STS :4566"]
    end

    subgraph BridgeZone["Backbone Steering Plane"]
        Connector["SdwanBackboneConnector"]
    end

    Branch -->|"HTTP + HMAC-SHA256 :8080"| API
    API -->|"HTTP REST Rego Eval :8181"| OPA
    API --> Connector
    Connector -->|"check_token"| Registry
    Connector -->|"boto3 EC2 & S3 :4566"| AWS
```

## Steering and Failure Sequence

```mermaid
sequenceDiagram
    autonumber
    participant B as SD-WAN Controller
    participant S as FastAPI Server :8080
    participant O as OPA :8181
    participant R as BranchLeaseRegistry
    participant C as SdwanBackboneConnector

    B->>S: POST /v1/sdwan/steer
    S->>S: verify_steering_payload
    alt Invalid HMAC or stale timestamp
        S-->>B: 401 Unauthorized
    else Signature valid
        par Policy evaluation
            S->>O: POST /v1/data/sdwan/backbone
            S->>O: POST /v1/data/sdwan/window
        end
        alt Policy denied
            S-->>B: 403 Forbidden
        else Policy allowed
            critical Lease acquisition
                S->>R: acquire(branch_key, holder_id)
            option Active conflict
                S-->>B: 409 Conflict
            end
            S->>C: steer_branch(fencing_token)
            alt Stale or expired token
                C-->>S: StaleSteeringToken
                S-->>B: 409 Conflict
            else Valid token
                C-->>S: SteeringResult
                S-->>B: 200 OK
            end
        end
    end
```

## Branch Lease State Machine

```mermaid
stateDiagram-v2
    [*] --> Released
    Released --> Leased: acquire() increments fencing_token
    note right of Leased: Token verified before EC2 Transit Gateway query and S3 write
    Leased --> Leased: same holder re-acquires or extends grace
    Leased --> Expired: expires_at_utc reached
    Expired --> Leased: new holder acquires higher fencing_token
    Leased --> [*]
```

## Trade-Offs and Limitations

Lease expirations and UTC change windows use UTC timestamps so Daylight Saving Time transitions never shift allowed hours. Off-window steering changes permit up to a 15-minute grace lock when an active incident ID is supplied. AWS requests retry transient errors up to 4 times with per-retry sleep capped at `2.0s`.

Limitations: `BranchLeaseRegistry` and `HashChainedLog` are held in single-process memory rather than a shared database, and `describe_transit_gateways` reads the first page without `NextToken` pagination.
