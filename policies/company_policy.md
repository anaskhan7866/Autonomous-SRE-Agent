# Company Remediation Policy (SAMPLE)

> SAMPLE POLICY. Replace this file with the real company policy, then run
> `python scripts/setup_policies.py` to re-index it. Each `##` section is one rule.

## POL-001: Restart of the affected service

RESTART of the affected service's own container is allowed for crashes, deadlocks, memory spikes and health-check failures. It is the preferred first remediation.

## POL-002: Pause and unpause

PAUSE and UNPAUSE of the affected service's own container are allowed for brief traffic isolation while the incident is investigated.

## POL-003: Stopping production containers

STOP of a production container is always forbidden for the agent; it requires a human change ticket. The agent must never propose STOP. When unsure, prefer RESTART.

## POL-004: Exec inside the container

EXEC runs a single program with plain arguments (no shell, quotes, pipes, wildcards or variables). It is limited to these read-only tools with these flags only: ls (-l -a -la -al -lh -h), cat (no flags), head and tail (only `-n N`), ps (no arguments, `-ef` or `aux`), df (-h), du (-s -h -sh), free (-h -m), and uptime, whoami, id (no arguments). Path arguments must be absolute and inside `/tmp`, `/var/log` or `/app/logs`; /proc, /sys, /dev, /etc, /root and every other location are forbidden to read, as are `.env` files. The only state-changing program is `rm` (flags -r, -f, -rf, -fr, -R, -v only) on paths strictly below `/tmp/` (for example `rm -rf /tmp/cache`), never `/tmp` itself and never with `.` or `..` segments. Any other program or flag is forbidden.

## POL-005: Always forbidden actions

Always forbidden: changing permissions or ownership (chmod, chown), installing packages, network tools (curl, wget, nc), downloading and running scripts, privilege escalation (sudo, su), killing PID 1, shutdown or reboot, touching the Docker socket or running docker inside containers, and editing system configuration.

## POL-006: Scope of action

Actions are allowed only on the affected service's own container. Any action on another container is forbidden, especially the `sre_agent` container itself.

## POL-007: Default deny

When a command is not clearly covered by an allowing rule, or the rules conflict, deny it.
