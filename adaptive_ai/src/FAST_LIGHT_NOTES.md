# Realtime fast-device timing

Interactive lights and simple binary targets use a realtime profile:

- action interval: 0.25 s maximum
- settling: 0.10 s maximum
- acknowledgement timeout: 2 s maximum
- manual changes are learned as feedback but do not create a timed control lockout

At startup, existing fast agents are migrated to this profile and stale persisted manual holds are cleared. This includes auto-created agents from older versions, which could retain the previous 30/60 second action interval.
