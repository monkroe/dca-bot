# CLAUDE.md — dca-bot

Crypto DCA automation (GitHub Actions / Python).
Part of the Robert OS ecosystem — see ~/robert-os-hub/CLAUDE.md for full context.

---

## Execution mode

Follows `AI-PLAYBOOK.md`'s CHAT-ONLY/REPO-CAPABLE mode split — this repo has no local override.

## Money

Roberto controls his money. Topping up accounts, transfer timing and the fate
of resting orders are his routine, not project tasks. Never write them as
open items, deadlines or reminders to him.

---

## Code constraints

- Stdlib-only Python: no external dependencies unless explicitly approved.
- Never commit secrets. All keys stay in GitHub Actions / Supabase secrets.

---

## Termux / Android constraints

- Claude Code runs inside the Ubuntu proot: `/tmp` exists and is writable
  (tmpfs), cleared on each new proot session -- nothing may be left there
  between sessions.
- Native Termux: `/tmp` exists but is NOT writable (owned by `shell`, mode
  `drwxrwx--x`). The writable temp dir is `$TMPDIR`
  (`/data/data/com.termux/files/usr/tmp`). Verified 2026-08-13.
- Bash through Claude Code works. If a script fails, diagnose the actual
  constraint before modifying the script or blaming the environment.
- Commands intended for Roberto to run must be single-line and copy-paste
  ready.
