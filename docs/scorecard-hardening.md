# 🛡️ OpenSSF Scorecard Hardening

How RADAR's OpenSSF Scorecard went from 4.2/10 toward ~8, and the manual steps
that finish the job. This is a maintainer runbook: revisit it whenever the
score plateaus or a new check regresses.

---

## 📚 Contents

- [🔭 Overview](#-overview)
- [📦 What Shipped (the Five Commits)](#-what-shipped-the-five-commits)
- [📈 Remaining to Reach ~8](#-remaining-to-reach-8)
- [🔐 Branch Protection (main and Release*)](#-branch-protection-main-and-release)
- [🏅 OpenSSF Best Practices (CII) Badge](#-openssf-best-practices-cii-badge)
- [🔎 Verifying the Score](#-verifying-the-score)

---

## 🔭 Overview

RADAR's OpenSSF Scorecard started at **4.2/10**. Five code commits raised the
fixable checks, and three manual, non-code steps (branch protection, the
OpenSSF Best Practices badge, and the time-based Maintained check) bring the
total to **~8.2**.

The README Scorecard badge reads the live `api.scorecard.dev` API. The legacy
`api.securityscorecards.dev` host that shields.io queries by default lacks
this repo's data, which is why the stock badge URL showed "invalid repo path."
Point the badge at `api.scorecard.dev` directly.

---

## 📦 What Shipped (the Five Commits)

| Check | Fix | Notes |
|---|---|---|
| Token-Permissions | Least-privilege, job-level `GITHUB_TOKEN` permissions in every workflow | Each job declares only the scopes it uses |
| Dependency-Update-Tool | Dependabot config with `open-pull-requests-limit: 0` | Keeps updates low-noise: Dependabot checks and reports without opening a PR flood |
| Security-Policy | `SECURITY.md` | Documents the private vulnerability-reporting process |
| Pinned-Dependencies | GitHub Actions pinned by commit SHA, Docker base images pinned by digest | Lands high but not a perfect 10 |

Pinned-Dependencies stops short of 10 because three `curl … \| sh` uv-installer
lines remain unpinned: `scripts/bootstrap.sh` and two lines in `ci.yml`. This
is an accepted trade-off, since pinning the installer script itself would mean
vendoring or hash-locking a third-party install script that changes with each
uv release.

---

## 📈 Remaining to Reach ~8

| Stage | Score |
|---|---|
| Baseline | 4.2 |
| After the 5 commits | ~6.5–6.7 |
| + Branch protection + CII badge | ~7.5 |
| + Maintained check (repo past 90 days old, steady commits) | ~8.2 |

The Maintained check is time-based: Scorecard looks at commit activity over a
rolling 90-day window, so it flips toward 10 on its own once the repo has that
history. No action is needed beyond continuing to commit.

The practical ceiling is **~8.5**. Code-Review needs approvals from a second
person, which a solo-maintained repo can't satisfy. The same constraint caps
Contributors and Fuzzing. These three are left as-is until a second maintainer
joins.

---

## 🔐 Branch Protection (main and Release*)

Configure via **GitHub → Settings → Rules → Rulesets**, covering `main` and
the `Release*` pattern.

| Setting | Scorecard credit |
|---|---|
| Restrict deletions | Branch-Protection |
| Block force pushes | Branch-Protection |
| Require a pull request before merging | Branch-Protection |
| Require status checks to pass | Branch-Protection |
| Require branches to be up to date before merging | Branch-Protection |
| Include administrators | Branch-Protection |

Required status checks (add from the dropdown after one PR run, so the names
match exactly):

- `lint + typecheck (ruff, mypy strict)`
- `test (full suite + no-silent-skip guard)`
- `helm lint + template + kubeconform (--strict)`
- `scan / osv-scan` (the OSV-Scanner reusable-workflow job)

Leave `scorecard` out of the required list: it runs only on push to `main`, so
it never reports on a PR branch and would block merges permanently.

**Solo-maintainer trade-off:** set required approvals to **0**. This keeps a
workable solo merge flow while still earning PR and status-check credit,
since a required-approvals setting above 0 would mean no PR can ever merge
(a GitHub author can't approve their own PR). Raise it to 1 (max Code-Review
credit) once a second maintainer is active.

---

## 🏅 OpenSSF Best Practices (CII) Badge

Register the project at [bestpractices.dev](https://bestpractices.dev) with
the repo URL and complete the passing-level questionnaire. Most of the ~66
criteria auto-pass from repo structure; the rest need a real answer with
evidence.

| Criterion | Evidence |
|---|---|
| FLOSS license | Apache-2.0: [`LICENSE`](https://github.com/k-kohli10/radar-system/blob/main/LICENSE) |
| Documentation | [`README.md`](https://github.com/k-kohli10/radar-system/blob/main/README.md) + [`docs/`](https://github.com/k-kohli10/radar-system/tree/main/docs) |
| Vulnerability reporting (private) | [`SECURITY.md`](https://github.com/k-kohli10/radar-system/blob/main/SECURITY.md) |
| Bug reporting | GitHub Issues + [`CONTRIBUTING.md`](https://github.com/k-kohli10/radar-system/blob/main/CONTRIBUTING.md) |
| Release notes | [`CHANGELOG.md`](https://github.com/k-kohli10/radar-system/blob/main/CHANGELOG.md) |
| Build system | [`Makefile`](https://github.com/k-kohli10/radar-system/blob/main/Makefile) + `uv` |
| Automated tests + test policy | `make test`; test requirement in [`CONTRIBUTING.md`](https://github.com/k-kohli10/radar-system/blob/main/CONTRIBUTING.md#-testing-expectations) |
| Lint / warnings addressed | `make lint` (ruff + mypy strict) |
| Credentials not leaked | Vault-only secrets + secret-leak CI hooks |
| Static analysis | ruff/mypy, SARIF uploaded to code scanning |
| Static analysis for vulnerabilities | OSV-Scanner workflow |
| Cryptography criteria | N/A: the project implements no cryptography of its own, it consumes TLS and Vault |

Dynamic analysis is suggested, not required, for the passing level.

---

## 🔎 Verifying the Score

After a merge to `main`, `scorecard.yml` republishes results. Re-query:

```
https://api.scorecard.dev/projects/github.com/k-kohli10/radar-system
```

Check the `.score` field and confirm each fixed check rose as expected.

The README badge color is set by hand to shields.io's scale (0-2 red, 2-5
yellow, 5-8 yellowgreen, 8-10 green), since shields' dynamic/json badge can't
color by the value it renders. Bump the color parameter each time the score
crosses a band.
