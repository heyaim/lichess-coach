# lichess-coach

MCP server and skill that connect Claude Code to a lichess account for
game analysis, puzzle statistics, and practice recommendations. The
repository is named `lichess-coach`; the MCP server, skill, and commands
are named `chess-coach`. Keep that as is.

## Installing for a user

Run `./setup`. It is interactive: it builds `.venv/`, offers a Stockfish
install, prompts the user to create their lichess token (they must do this
step themselves; never handle their lichess password), registers the MCP
server with Claude Code, and links the skill. Verify with
`claude mcp list` (expect `chess-coach ... ✔ Connected`), then have the
user test in a new session.

## Layout

- `coach_mcp.py` - the MCP server (stdio, newline-delimited JSON-RPC).
  Launcher: `./coach-mcp`.
- `core.py` - shared plumbing: lichess access, request pacing, puzzle
  digests, statistics, local data files.
- `debrief.py` - the game analysis engine.
- `skill/chess-coach/SKILL.md` - coaching persona, symlinked into
  `~/.claude/skills/`.
- `tests/` - unit tests for request pacing, attempt counting, and the
  tools that parse lichess responses (standard library only).
- `data/`, `reports/`, `.lichess-token` - user data; gitignored, never
  commit or overwrite.

## Invariants

- Fair play: `analyze_game` refuses in-progress games. Keep it that way,
  and never add features that assist ongoing games against people.
- Lichess API: every lichess call follows the same rules: one request at
  a time, a proper User-Agent, and the one-minute wait after a 429. The
  MCP server uses `rest_request` in `core.py`, and the CLI fetch in
  `debrief.py` implements the same rules. Do not weaken them, in this
  repo or in forks.
- `coach_mcp.py` must not print to stdout; stdout carries the protocol
  (stray prints are redirected to stderr at startup, keep that).
- Python 3.9 compatibility throughout; no new runtime dependencies without
  strong cause.

## Checks

`./.venv/bin/python -m py_compile coach_mcp.py core.py debrief.py` and
`./.venv/bin/python -m unittest discover -s tests` must both pass.
Protocol changes: test with a stdin/stdout JSON-RPC harness (initialize,
tools/list, tools/call) before shipping.
