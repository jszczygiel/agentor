# Fix F401 unused-import errors in tests/test_config.py — 2026-04-17

## Gotchas for future runs
- Repo has no git remote, so `.github/workflows/ci.yml` never actually runs. Ruff config is correct (defaults catch F401) but drift still lands because no PR gate executes. Agents running ruff locally will rediscover lint debt that "CI" can't catch.

## Follow-ups
- None filed. Wiring up a remote / self-hosted CI is out of scope; the backlog item already signaled the symptom.
