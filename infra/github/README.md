# GitHub repo hardening (deferred — needs plan/scope)

Two Packet-0 steps require account-level access the bootstrap token lacks.

## 1. Push the CI workflow (needs `workflow` token scope)

The pipeline `.github/workflows/ci.yml` is committed locally but unpushed; the
bootstrap OAuth token lacked `workflow` scope. Grant it and push:

```
gh auth refresh -s workflow          # interactive; adds workflow scope
git push origin main                 # sends the CI workflow commit
```

## 2. Enable branch protection (needs public repo or GitHub Pro/Team)

Branch protection + rulesets are blocked on private repos on the Free plan.
Upgrade to Pro (keeps the repo private), then apply the ruleset — require a PR,
a green `ci` check, code-owner review, and block force-push/deletion:

```
gh api -X POST repos/patelaryia/pacha_insurance/rulesets \
  --input infra/github/main-ruleset.json
```

`.github/CODEOWNERS` is already in place and activates the moment the ruleset is
enabled. `bypass_actors` grants the Admin role an always-bypass so you can
bootstrap without deadlocking on the first CI run.

<!-- ruleset bypass probe -->
