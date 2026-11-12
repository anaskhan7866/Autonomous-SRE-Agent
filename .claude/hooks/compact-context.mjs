// Compaction hooks for the lead session, teammates and subagents.
//   pre  (PreCompact): stdout becomes extra instructions for the compaction summary.
//   post (SessionStart, matcher "compact"): re-injects the agent's persistent notes and the
//        list of modified files right after compaction.
// Never fails the session: any error just prints nothing and exits 0.
import { readFileSync, existsSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { join } from "node:path";

const MAX_CHARS = 9000; // hooks cap injected text at 10,000 characters
const root = process.env.CLAUDE_PROJECT_DIR || process.cwd();

function readStdin() {
  try {
    return JSON.parse(readFileSync(0, "utf8") || "{}");
  } catch {
    return {};
  }
}

function safeName(agentType) {
  const name = String(agentType || "lead").toLowerCase();
  return /^[a-z0-9-]{1,64}$/.test(name) ? name : "lead";
}

function modifiedFiles() {
  try {
    const out = execFileSync("git", ["status", "--short"], { cwd: root, encoding: "utf8", timeout: 5000 });
    return out.trim() || "(working tree clean)";
  } catch {
    return "(git status unavailable)";
  }
}

const PRE = `When summarising this conversation, preserve explicitly, in this order:
1. Current task: the user's latest request, verbatim if short, and its status.
2. Progress: what is done, what is in progress, and which agents are still running.
3. Files modified: every file created or changed, with one line on why.
4. Decisions: important implementation and design decisions, and the user's choices.
5. Errors and unresolved issues: failures, open review findings and known limitations.
6. Tests executed: exact commands and pass/fail counts.
7. Next steps: the concrete next action.
Keep the user's standing constraints (CLAUDE.md, .claude/rules/development.md). Drop verbose tool output.`;

function post(input) {
  const name = safeName(input.agent_type);
  const notesPath = join(root, ".claude", "notes", `${name}.md`);
  const notes = existsSync(notesPath) ? readFileSync(notesPath, "utf8").trim() : "";
  const text = [
    `[Context restored after compaction for: ${name}]`,
    notes ? `Persistent notes (.claude/notes/${name}.md):\n${notes}` : `No persistent notes at .claude/notes/${name}.md.`,
    `Modified files (git status --short):\n${modifiedFiles()}`,
  ].join("\n\n").slice(0, MAX_CHARS);
  process.stdout.write(JSON.stringify({ hookSpecificOutput: { hookEventName: "SessionStart", additionalContext: text } }));
}

try {
  const mode = process.argv[2];
  const input = readStdin();
  if (mode === "pre") process.stdout.write(PRE);
  else if (mode === "post") post(input);
} catch {
  // never block or break compaction
}
process.exit(0);
