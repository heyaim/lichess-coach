# lichess-coach

[![ci](https://github.com/heyaim/lichess-coach/actions/workflows/ci.yml/badge.svg)](https://github.com/heyaim/lichess-coach/actions/workflows/ci.yml)

lichess-coach links Claude to your lichess account so Claude can coach
you on your games and puzzles, showing you where you can improve.

- "What happened in my last game?" - it fetches the game, checks every
  move with Stockfish (the engine lichess uses), and tells you the story:
  where it was decided, what the better plans were, and one thing to work
  on.
- "Summarize my puzzle performance." - statistics from your actual record,
  such as "forks: 68% missed over 37 seen," with a link to train exactly
  that.
- "What should I practice?" - it names your weakest patterns from your
  performance and links you to practice on lichess, including replaying
  the exact puzzles you failed, in lichess's normal puzzle screen.
- "What have I done this week?" - your daily activity: puzzle volume
  including replays, practice sessions by name, and Puzzle Storm.
- "Let's work on the puzzles I missed." - it pulls your missed puzzles
  and walks them with you one at a time, hints first.
- "Why does this puzzle move work?" - a walkthrough of any lichess puzzle,
  including why the tempting wrong move fails.
- Show it any position, as a screenshot or in chess notation. Once the
  position is read, the analysis comes from Stockfish.
- Ask anything else about chess - rules, terms, why a move works - and the
  answers match your level and use what you've played.

## Setup

Requires macOS or Linux, [Claude Code](https://claude.com/claude-code),
and a [lichess](https://lichess.org) account. The coach runs on your
computer, in Claude Code. Claude in your browser and the Claude mobile
app cannot run or install it. Paste into a Claude Code session:

> Clone https://github.com/heyaim/lichess-coach, run its setup script, and
> connect the chess coach.

Setup includes creating a lichess personal access token so the coach can
read your puzzle activity; the token creation page opens with the
required permission selected. Details, verification, and uninstall are in
**[INSTALL.md](INSTALL.md)**. When it's done, ask a new Claude session
"is my chess coach connected?"; the answer should name your lichess
account.

## Privacy

Everything stays on your computer: your lichess token, the analysis, the
reports. The coach talks only to lichess and runs on your existing Claude
plan. No accounts, no server, nothing hosted.

## Fair play

Lichess forbids outside assistance during games. The coach analyzes
finished games and reviews your puzzle history. The analysis tool
refuses games that are still in progress, and position analysis refuses
the current position of any game against a person.

## License

MIT. lichess-coach is an independent project, not affiliated with or
endorsed by lichess.
