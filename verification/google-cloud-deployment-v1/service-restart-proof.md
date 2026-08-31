# systemd Service Restart Proof (Acceptance #11)

## Mechanism
`trace-run.service` has `Restart=always` + `RestartSec=30`. Killing the main
process must cause systemd to relaunch it automatically.

## Test (restart_test.sh)
Killed the running main process with SIGKILL, waited 40s, re-queried systemd.

```
PID_BEFORE=6116
PID_AFTER=6314
AUTO_RESTART_OK
active
```

journal:
```
Aug 26 13:21:03 trace-vm systemd[1]: Stopped Trace event radar pipeline ...
Aug 26 13:21:03 trace-vm systemd[1]: Started Trace event radar pipeline ...
Aug 26 13:21:05 trace-vm trace-run[6314]: ... pipeline started (poll=60s)
```

## Conclusion
PASS — systemd automatically restarted the service after a hard kill
(PID changed 6116 → 6314), unit remained `active`.
