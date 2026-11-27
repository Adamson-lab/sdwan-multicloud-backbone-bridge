package sdwan.backbone

import rego.v1

default allow := false
default reason := "denied: backbone target or SLA steering class not permitted"

allow if {
	input.cloud_backbone in {"aws-tgw", "azure-vwan", "gcp-ncc"}
	input.sla_class in {"low-latency-voice", "bulk-replication", "standard-branch"}
	input.bgp_asn >= 64512
	input.bgp_asn <= 65534
}

reason := "allowed: SD-WAN branch steered onto approved multicloud backbone" if {
	allow
}
