# VM Reboot Proof — Boot Recovery & Persistence (Acceptance #12 & #10)

## Action
`gcloud compute instances reset trace-vm --zone=us-central1-a`
(then confirmed status → RUNNING)

## Before reboot (db_state.sh)
```
events: 193
raw_items: 222
cursors: 8
source_health: 13
uptime: 13:21:49 up 1:42
```

## After reboot (db_state.sh)
```
events: 193
raw_items: 222
cursors: 8
source_health: 13
uptime: 13:23:54 up 1 min     <- machine really rebooted
```
```
systemctl is-active trace-run  -> active
systemctl is-enabled trace-run -> enabled
```

## Conclusion
PASS —
1. After a real VM reboot (uptime reset to ~1 min), `trace-run` came back
   `active`/`enabled` automatically (boot recovery).
2. SQLite DB, collector cursors, events, and source_health counts are IDENTICAL
   before and after reboot → persistent-disk storage confirmed.
