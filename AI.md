# AI Start Here (DCA Bot)

Before proposing changes, read and follow:
- `monkroe/robert-os-hub/AI-PLAYBOOK.md` (canonical protocol)
- `docs/05-roadmap/dca-bot-v2.3.md` in the hub (canonical spec)

## Non-negotiables (summary)
- One step at a time; stop and wait for user "Done"
- No code/patch/SQL unless explicitly commanded
- Always run `./test.sh` before any commit
- Stdlib-only Python (no external deps) unless explicitly approved
- Never commit secrets; all keys must stay in GitHub/Supabase secrets

## Default next step
Start with inspection (`rg` / `sed -n` / `nl -ba`), then propose ONE command.
