# Design decision: scale-to-zero, not an always-on daemon

Deliberately **socket-activated**, not a resident systemd service. Same
lazy-load/idle-unload shape as serverless cold starts, via systemd's own
primitives:

- `vinowhisper-server.socket` owns the listening port at boot. No model
  loaded, no Python process running.
- Systemd starts `vinowhisper-server.service` on the _first_ connection. That
  is when the NPU model load (~10-30s) happens.
- The service tracks its own last-request time and self-exits after
  `config.IDLE_TIMEOUT_S` (30 min). The socket unit is untouched, so the next
  caption session respawns it.

Why bother on a 16GB machine for a ~500MB model: the always-on version holds
that RAM resident regardless of use, and relying on the kernel to swap it to
zram does not reliably help, since 500MB rarely generates enough pressure on
16GB to get reclaimed. Scale-to-zero is the deterministic version of the same
idea. Whether 30 minutes is the right window, or whether this is solving a
problem too small to matter at ~3% of 16GB, is still open. It is a conscious
choice, not a default that snuck in.

`vinowhisper-caption` health-checks the server before starting the loop, so a
cold start shows up as an explicit "waiting for the transcription server"
line rather than as the captions appearing to be broken for 30 seconds.

**Stopping it.** There's no daemon to manage day to day. The socket unit
holds the port with no process behind it until something connects, and the
service self-exits after `IDLE_TIMEOUT_S` regardless. Two commands cover the
rest:

```bash
systemctl --user stop vinowhisper-server.service          # drop the resident NPU process now
systemctl --user disable --now vinowhisper-server.socket  # full teardown
```

The socket unit is what respawns the service, so disabling it (not just the
service) is the one to use before a reboot or when you're done with the tool
for a while, otherwise the next `vinowhisper-caption` run just spawns it
again on first connection.
