package sdwan.window

import rego.v1

default allow := false
default reason := "denied: steering change outside UTC maintenance window without active grace lock"

# Allow off-window steering changes up to 15 minutes during active incident mitigation.
grace_active if {
	input.grace_lock.active == true
	count(input.grace_lock.incident_id) >= 4
	input.grace_lock.extra_minutes <= 15
	input.actor_role in {"sdwan-ops", "incident-cmd"}
}

allow if {
	grace_active
}

allow if {
	not grace_active
	input.actor_role in {"sdwan-ops", "incident-cmd", "sdwan-bot"}
	input.utc_hour >= 1
	input.utc_hour <= 23
}

reason := "allowed via active 15m incident grace lock" if {
	allow
	grace_active
}

reason := "allowed within UTC SD-WAN change window" if {
	allow
	not grace_active
}
