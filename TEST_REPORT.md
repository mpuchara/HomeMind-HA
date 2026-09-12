# Test report — 0.7.12

73 unit/contract tests pass. New regressions cover: entity-level electrical filtering by unit only; non-electrical siblings on meter devices remaining eligible; `device_class=power` without an electrical unit remaining eligible; arbitrary camera/phone/car/person/calendar/hidden-diagnostic entities entering the candidate pool when historically relevant; LD2411 percentage radar energy remaining eligible; and the existing lifecycle/control/short-series paths.
