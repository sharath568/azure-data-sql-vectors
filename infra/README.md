# Azure infrastructure (free offer)

`main.bicep` deploys the smallest setup that runs everything in this repo on
Azure, and for most people it costs nothing:

| Resource | Setting | Why |
|---|---|---|
| Logical SQL server | SQL auth, TLS 1.2, public endpoint | Simple for a demo |
| Firewall rule | One client IP (`clientIp`) | No open-to-the-world access |
| Database | **GP_S_Gen5_2 serverless**, min 0.5 vCores, 32 GB, local backup redundancy | The free offer's shape |
| Free offer | `useFreeLimit: true`, `freeLimitExhaustionBehavior: 'AutoPause'` | When the monthly allowance is gone the database pauses until the 1st. It can't bill you |
| Auto-pause delay | `autoPauseDelayMinutes`, default 60 | 15 is the minimum and saves vCore-seconds (see below) |
| Azure OpenAI (optional) | `text-embedding-3-small`, pay per token | Only with `deployOpenAi=true` |
| Budget | Resource group, default 5, alerts at 50 % / 100 % actual and 100 % forecast | An alert, not a cap |

## Deploy

```bash
az login
BUDGET_EMAIL=you@example.com ./scripts/up.sh            # database only
BUDGET_EMAIL=you@example.com DEPLOY_OPENAI=true ./scripts/up.sh   # + embeddings
cp .env.azure .env
sqlvec doctor
sqlvec init
sqlvec ingest examples/docs
sqlvec search "how do I keep the database free?"
./scripts/down.sh                                         # when you're done
```

`up.sh` creates the resource group (`sqlvec-rg` in `eastus2` unless you set
`RG` / `LOCATION`), detects your public IP, generates an admin password,
deploys the template and writes `.env.azure` (mode 600, git-ignored).

## The cost guardrails

My whole Microsoft MVP lab runs on a budget of under 100 USD of Azure spend
across about a dozen projects. These are the rules that keep it there, and
this template follows them:

1. **Build and test locally first.** Everything in this repo runs against
   SQL Server 2025 Developer edition in Docker for free. Go to Azure for the
   "proof run" only.
2. **Prefer offers that can't overspend.** The Azure SQL free offer with
   `AutoPause` is the best kind of free: when it runs out, it stops. Never
   flip a free database to "continue with charges"; you can't flip it back.
3. **A budget on every resource group, the same day you create it.** Azure
   budgets e-mail you; they don't stop anything. Act on the first mail.
4. **Throwaway resource groups.** One group per experiment, deleted
   (`scripts/down.sh`) the same day the numbers are captured.
5. **Pay-per-token AI only.** No provisioned throughput, no always-on
   compute. Embedding the sample docs costs a fraction of a cent.
6. **Check what's still running**, weekly at least:
   `az resource list -o table` and the Cost Management view for the
   subscription.

## How far 100,000 vCore-seconds go

`sqlvec cost` does the maths. The short version: the database bills per
second for whichever is bigger, the vCores you use or a floor of
`max(min vCores, min memory / 3)`. For GP_S_Gen5_2 at 0.5 min vCores that
floor is 2.05 GB / 3 = **0.683 vCores**, and you keep paying it while the
database sits idle waiting to auto-pause.

```text
$ sqlvec cost --sessions 20 --active-minutes 30
...
| delay (min) | vCore-seconds | % of free | sessions that fit |
|---:|---:|---:|---:|
| 15 | 48,300 | 48.3% | 41 |
| 30 | 60,600 | 60.6% | 33 |
| 60 | 85,200 | 85.2% | 23 |
```

That's 30-minute working sessions at about 1 vCore. With the default
60-minute delay, the idle tail (49,200 vCore-seconds) costs more than the
work (36,000). Dropping the delay to 15 minutes would nearly double the
number of sessions that fit, **but the free offer doesn't allow it.** I
tried `AUTOPAUSE_DELAY=15 ./scripts/up.sh` on 2026-10-11 and the deployment
failed with "Only default value for auto pause delay is allowed for Free
Limit database with auto pause exhaustion behavior". With
`freeLimitExhaustionBehavior: 'AutoPause'` you get 60 minutes. A shorter
delay only works on a normal (billed) serverless database, which is a trade
I wouldn't make for a demo: the idle tail is cheaper than losing the
can't-overspend guarantee. The fastest way to stop the idle meter is to
delete the database when you're done.

A 5k x 384 `sqlvec bench` run is a few minutes of work, so a handful of
bench runs a month fits comfortably.

## Things to double-check before you deploy

- **Free offer availability.** Up to 10 free databases per subscription.
  If you've used them, the deployment fails (it won't silently create a
  paid one as long as `useFreeLimit=true`).
- **DiskANN on Azure SQL Database is GA.** No `PREVIEW_FEATURES` needed
  there. Check `sqlvec doctor` for the index version after `sqlvec index`.
- **Full-text is built in** on Azure SQL Database, so hybrid search works
  there without the extra container image.
- **API versions:** `Microsoft.Sql` 2023-08-01, Cognitive Services
  2024-10-01, budgets 2023-11-01. The template builds and lints cleanly
  with Bicep 0.48.1 (`bicep build`, `bicep lint`), and CI checks that on
  every push. I deployed it to `centralus` on 2026-10-11 (database) with
  Azure OpenAI in `eastus`; the results are in
  [docs/bench/](../docs/bench/) and [docs/azure-run.md](../docs/azure-run.md).
- **Model availability:** not every region offers `text-embedding-3-small`
  with the `Standard` deployment type. If it fails, set
  `EMBED_SKU=GlobalStandard` or another `OPENAI_LOCATION`.
- **One Azure OpenAI account per region on a Free Trial.** The trial
  subscription I used had an `OpenAI.S0.AccountCount` limit of 1 per region
  (`az cognitiveservices usage list -l <region>`). If another project
  already has an account in your region, the deployment fails preflight
  with `InsufficientQuota`; set `OPENAI_LOCATION` to another region.
- **macOS: OpenSSL for the driver.** `mssql-python`'s bundled driver needs
  OpenSSL 3. With Homebrew's `openssl@3`, the first connection failed with
  "OpenSSL library could not be loaded" until I set
  `DYLD_LIBRARY_PATH=/opt/homebrew/opt/openssl@3/lib`. macOS drops that
  variable when it runs `/bin/sh` scripts, including the `sqlvec` console
  script in a venv whose path contains a space; `python -m sqlvec` keeps it.
- **Prices** in this repo are reference values. Use the
  [Azure pricing calculator](https://azure.microsoft.com/pricing/calculator/).

## Security notes

SQL auth with a generated password and a single-IP firewall rule is fine
for a throwaway demo. For anything real, use Microsoft Entra ID
authentication, private endpoints, and a managed identity for the external
model instead of an API key.
