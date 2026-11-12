# Persistent agent notes

Short working notes that must survive context compaction. After any compaction, the
`SessionStart` hook (`.claude/hooks/compact-context.mjs`) re-injects `<agent>.md` together with
`git status --short`.

| File | Written by |
|---|---|
| `lead.md` | team lead (main session) |
| `backend-developer.md` | backend-developer |
| `frontend-developer.md` | frontend-developer |
| `qa-engineer.md` | qa-engineer |

security-reviewer and code-reviewer are read-only. They don't write notes and put everything in their reports instead.

Keep each file under about 60 lines and overwrite stale content, so the file is a snapshot, not a log:

```
## Current task
## Progress
## Files modified
## Decisions
## Errors / unresolved
## Tests run
## Next steps
```

Never put secrets, `.env` values, webhook URLs or tokens here. The `*.md` notes are git-ignored; only this README is committed.
