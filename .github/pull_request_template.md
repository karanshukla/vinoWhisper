## Summary

<!-- What changes, and why. -->

## Testing

- [ ] `uv run poe check` passes (ruff, format, mypy, pytest)
- [ ] Ran it on hardware, or said below why that wasn't possible

Most of this project cannot be verified in CI — there is no NPU, no audio
server and no OpenVINO there. Say which of these applies:

- [ ] Verified on the NPU (captions ran)
- [ ] Verified with `vinowhisper-replay --restitch` against a recorded session
- [ ] Verified with `vinowhisper-doctor`
- [ ] Logic only; the hardware path is unchanged

## Distro impact

- [ ] Touches `distro.py` package names — say which distro they were checked on
- [ ] Touches capture (`capture.py`) — say which backend was exercised
- [ ] Neither

## Checklist

- [ ] Comments explain *why*, not what, and any measured claim carries a date
- [ ] README / CLAUDE.md updated if behaviour or setup changed
- [ ] No secrets, tokens or machine-specific paths committed
