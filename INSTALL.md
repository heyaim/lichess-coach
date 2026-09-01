# Setup

lichess-coach connects Claude Code to your lichess account. Claude can
then analyze your games, report your puzzle strengths and weaknesses,
point you at the right practice on lichess, explain any position, and
answer chess questions using your own record.

## Requirements

- macOS or Linux
- [Claude Code](https://claude.com/claude-code)
- A [lichess](https://lichess.org) account

## Install

Paste into a Claude Code session:

> Clone https://github.com/heyaim/lichess-coach, run its setup script, and
> connect the chess coach.

Or run it manually:

```bash
git clone https://github.com/heyaim/lichess-coach.git
cd lichess-coach && ./setup
```

Setup prompts for a lichess personal access token and opens the creation
page with the required permission selected. The token grants read access
to your puzzle activity. Games are fetched from lichess's public export.
The token is stored locally in `.lichess-token`
(excluded from git) and can be revoked at
lichess.org/account/oauth/token.

## Verify

In a new Claude Code session:

> Is my chess coach connected?

The response should name your lichess account. Things to try after that:
"Analyze my last game." "Summarize my puzzle performance." "What should I
practice?" "Explain this position," with a screenshot or notation.

## Troubleshooting

Ask Claude to check the chess coach connection, or see the reference
below.

## Uninstall

Ask Claude to remove the chess coach, then delete the repository folder.
Revoke the token at lichess.org/account/oauth/token.

---

## Reference

Setup creates `.venv/` (python-chess, requests), installs Stockfish on
approval (Homebrew on macOS; on Linux install via the package manager
first), stores the token in `.lichess-token`, registers the MCP server
(`claude mcp add --scope user chess-coach -- <dir>/coach-mcp`), symlinks
`skill/chess-coach` into `~/.claude/skills/`, and runs a self-check.

Verification: `claude mcp list` should report
`chess-coach ... ✔ Connected`. New sessions only; running sessions do not
load new tools.

Common failures:

- **Not connecting**: run `./coach-mcp` directly. It should wait silently
  on stdin (Ctrl-C to exit). Output indicates the cause; typical causes
  are a missing `.venv` (re-run `./setup`) or Python older than 3.9.
- **No token**: re-run `./setup`, or write the token to `.lichess-token`.
- **HTTP 429 from lichess**: the API allows one request at a time; the
  budget is shared with any other lichess tools running. Retry after a
  minute.
- **Stockfish missing**: install it, then start a new session.

Manual uninstall:

```bash
claude mcp remove --scope user chess-coach
rm ~/.claude/skills/chess-coach
```
