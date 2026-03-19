# CLAUDE.md — dca-bot

Crypto DCA automation (GitHub Actions / Python).
Part of the Robert OS ecosystem — see ~/robert-os-hub/CLAUDE.md for full context.

---

## Termux / Android Constraints

- `/tmp` is NOT writable in Termux — all bash tools that use `/tmp` will fail with `EACCES: permission denied`
- This means test scripts cannot be run via Claude Code bash tools
- **Do NOT attempt to run bash tools** — they will always fail
- Instead: generate the correct single-line command and ask the user to run it in Termux manually
- User will paste the output back if needed
- This is a hard Android limitation, not a configuration issue — do not try to fix it
