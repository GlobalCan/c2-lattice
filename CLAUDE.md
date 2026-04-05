# C2 Lattice — Agent Coordination

You have access to **c2-lattice** MCP tools for coordinating multi-agent work.

## When to use C2 Lattice

When the user says any of: "go execute", "build it", "go", "let's do it", "start building", "execute the plan", "spin up workers", "delegate this" — switch from planning to execution using the c2-lattice tools.

## Execution workflow

1. **Call `list_peers`** to register yourself with the broker
2. **Call `create_task`** for each piece of work from the plan you discussed. Use `blocked_by` for dependencies.
3. **Call `spawn_worker`** to open 2-3 worker sessions (you must be registered as architect)
4. **Wait ~20 seconds** for workers to boot
5. **Call `send_message`** to each worker with their assignment: tell them to `list_tasks`, `claim_task`, work, `complete_task`
6. **Monitor** with `list_tasks` and `check_messages` — report progress when asked
7. **When all tasks are done**, summarize results to the user

## Status updates

When the user asks "status?", "how's it going?", "update?", "sitrep" — call `list_tasks` and `check_messages`, then give a concise summary: what's done, what's in progress, what's blocked.

## Key rules

- Plan first, execute after the user confirms
- Don't use the Agent tool for parallel work — use `spawn_worker` instead
- Each task needs a clear title and description so workers know what to build
- Workers auto-register and check for tasks — you just need to send them an initial message
- If a worker raises a blocker, relay it to the user
- Use `set_memory` to store decisions from the planning phase so workers can access them
