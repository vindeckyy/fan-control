## Summary

<!-- What changed and why? -->

## Validation

- [ ] `python3 -m py_compile fan_backend.py fan_policy.py fan_runtime.py fan_controller.py fan_rpc.py fan_diagnostics.py fan-daemon.py fan-gui.py fan-ctl.py fan_gtk.py test_fan_control.py`
- [ ] `python3 -m unittest -v`
- [ ] `make lint`
- [ ] `python3 scripts/check_versions.py`
- [ ] Dashboard changes were checked in `--demo` mode
- [ ] Documentation was updated where behavior changed

## Hardware impact

<!-- Models tested, EC reads/writes affected, safety bounds, and untested paths. Use "None" for non-hardware changes. -->

## Unofficial project acknowledgement

- [ ] I understand this is an unofficial community project with no manufacturer endorsement or support.
