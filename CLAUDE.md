# tg-mcp-bridge

Remote MCP server + OAuth + Telegram bot: Claude sends messages and quizzes to a user in Telegram.

## Language

- Everything that goes into the repository is in English: code, comments, docs (including this file, SPEC.md, PLAN.md), commit messages, branch names, PR titles and descriptions, PLAN.md log entries, and user-facing texts (bot messages, HTML pages, MCP tool descriptions).
- Conversation with the operator in the session is in the language the operator wrote in. If the operator writes in Russian, reply in Russian, but anything you write into the repo or to GitHub is still in English.

## Secrets (hard rule)

- Never read, create, edit, copy, source or list `.env` or `.env.*`. The only exception is `.env.example`.
- `.env.example` contains placeholders only: secret keys empty or `<placeholder>`. Never put a real-looking token, id or key there, even as an "example".
- Local runs use `ENV_FILE=.env.example`. Tests set config via fixtures, never via env files.
- Never modify `.claude/settings.json` or `.claude/hooks/**`. If a guard blocks something you think is needed - stop and ask the operator, do not look for a workaround.
- Manual checks that need real secrets are run by the operator: give the exact command and wait for the result.
- Details and enforcement layers: SPEC.md, "Secrets".

## Git flow

- `main` - stable releases, `dev` - integration branch. Never commit directly to `main` or `dev`.
- Each step in PLAN.md is a separate branch off `dev`: `step/NN-<short-name>`, e.g. `step/01-skeleton`, `step/05-oauth`.
- A step ends with a PR from the step branch into `dev`. Only the operator merges, using squash merge: one commit per step on `dev`.
- Inside a step branch, any number of commits is fine (Conventional Commits: `feat:`, `fix:`, `test:`, `docs:`, `chore:`).
- Releases `dev` → `main` are done by the operator via a separate PR (e.g. after step 8 - MVP, and after step 12).

## Start of every session

1. `git fetch --all --prune`, `git checkout dev`, `git pull --ff-only`.
2. `gh pr list --base dev --state open`. If there is an open PR - stop: give the operator its number and link, and do not start a new step until it is merged or the operator says otherwise. If the operator asks to work on the open PR (e.g. review comments) - switch to its branch and work there.
3. Check `git status`: the working tree must be clean. If not - ask the operator what to do.
4. Read `SPEC.md` in full (it is short): goals, stack, env, data model, invariants.
5. In `PLAN.md` read only the "Status" table and the latest "Log" entries. The current step is the first one that is not `done`. If the operator named another step - work on that one.
6. Read only the current step's section of `PLAN.md`: find its line with `grep -n "^## Step" PLAN.md` and read that line range. Do not read other steps unless the task requires it.
7. If a `step/NN-...` branch for this step already exists on the remote (a previous session did not finish) - continue on it. Otherwise create a new one from the fresh `dev`.
8. In the step branch, set the step status to `in progress`.

## Rules

- Steps strictly in order. Do not move on until the current step's validation passes. If validation fails - fix it, do not work around it.
- One session = one step = one branch = one PR. Do not touch code unrelated to the step, except small necessary fixes (list them in the PR).
- Manual checks that are impossible right now (e.g. before deployment) do not block a step: note them as deferred in the log and in the PR.
- The MCP SDK API changes fast: before steps 5-6, check the current docs and examples for `mcp` (Python SDK, `mcp.server.auth`), do not rely on memory. If the SDK does not cover something in the spec - implement it by hand following the MCP Authorization specification.
- If you find a contradiction or a gap in SPEC.md or PLAN.md - stop and ask, do not silently fill it in.
- Changes to SPEC.md and PLAN.md (other than status and log) only with the operator's consent.
- Secrets (`.env`, tokens) never go into commits, PRs or PR descriptions.

## End of step (acceptance = validation passed)

1. All tests are green (`pytest`), the step's validation passes.
2. In `PLAN.md` on the step branch: status → `done`, an entry in "Log" (2-5 lines).
3. Commit, `git push -u origin step/NN-...`.
4. Create the PR right away: `gh pr create --base dev --title "Step N: <title>"`. Description:
   - **What was done** - short bullet points.
   - **Validation** - which checks passed, test output in one or two lines.
   - **Open / deferred** - what was not verified and why.
   - **Manual actions needed** - if any (DNS, BotFather, env on the server).
5. Wait for CI on the PR (`gh pr checks --watch`). If CI is red - fix it on the same branch.
6. Send the operator the PR link and the same short summary (in the operator's language). Do not merge it yourself.

If a step is blocked (needs an operator decision or an external action): status `blocked` with the reason, push, open the PR as a draft (`gh pr create --draft`) describing the blocker, and stop.

## Style

- Use regular hyphens, no em-dashes, in code, comments and docs.
