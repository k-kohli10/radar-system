# Git workflow

## Staging (after approval only)

- After I approve a completed unit of work, stage **exactly the files belonging
  to that unit, using explicit file paths**: `git add path/one path/two`.
- **Never** `git add -A`, `git add .`, or any glob/wildcard staging.
- After staging, **list the staged files back to me** along with the **proposed
  conventional commit message**. I review the staged set, then run `git commit`
  and `git push` myself.
- **All other git operations are mine.** You do not run `commit`, `push`,
  `branch`, `merge`, `rebase`, `reset`, or `stash`.

## Working from tasks

- **Prefer source file references over pasted code blocks.** When I give you a
  task, read the referenced files directly rather than relying on pasted
  snippets.

## Commit hygiene

- **Prior-phase bug fixes get their own standalone, clearly-labeled commit**
  (e.g. `fix: ...`) — never buried inside feature work. See the a288a4e pattern
  described in [phase-gates.md](phase-gates.md).
- **Documentation cleanup goes in a single `docs:` commit at phase end**, not
  scattered across mid-phase commits.
- **Propose the commit message with each completed unit of work**, in
  Conventional Commits format, one logical unit per commit (not per file).
