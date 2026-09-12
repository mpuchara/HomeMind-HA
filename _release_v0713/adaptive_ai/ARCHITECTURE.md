# Adaptive AI 0.7.12 architecture notes

Context admission is deliberately broad. The candidate universe is every parseable Home Assistant state, regardless of domain, name, device class, hidden/diagnostic category or vendor. Two hard exclusions remain: (1) detected controllable entities and all Entity Registry siblings on those actuator devices, and (2) individual entities whose `unit_of_measurement` is explicitly electrical.

Electrical filtering is entity-level and unit-only. A `Still Energy` sensor in `%`, a camera score without a unit, a virtual `power` score without W/VA/etc., or a non-electrical sibling on a Shelly device remains eligible. Only the electrical-unit entity itself is blocked.

Historical indexing evaluates the broad pool, but live policies remain compact. Fast binary targets select a small set of historically predictive inputs and encode each as current value plus ~1 s, 3 s and 10 s deltas. Generic historical precursor scoring can promote arbitrary phone/car/weather/template context; dedicated occupancy/activity edge scoring remains an extra fast-path for presence/radar/AI drivers.

A training-revision upgrade performs one broad Recorder candidate refresh, then steady-state maintenance returns to the selected context of QUALIFIED agents. PAUSED agents consume no normal inference/training CPU until Resume/Rebuild.
