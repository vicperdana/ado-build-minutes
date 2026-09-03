# ado-build-minutes

`ado-build-minutes` reconstructs Azure DevOps pipeline/build minutes across many Azure DevOps organisations and breaks them down by runner type.

Azure DevOps bills primarily by **parallel jobs**, not historical minutes. Paid Microsoft-hosted and self-hosted parallel jobs are capacity licences, and Azure DevOps does not provide a native cross-organisation report for “total build minutes by runner type”. This tool gathers that evidence from Azure DevOps REST and Analytics OData APIs, normalises it, reconciles overlapping sources, and emits CSV, JSON, and Markdown summaries suitable for an executive customer response.

**Status: experimental.** This is an independent, community-maintained tool. It is not affiliated with, endorsed by, or supported by Microsoft or GitHub. It relies in part on undocumented Azure DevOps endpoints that may change without notice. Output should be independently validated before being used for financial, contractual, or commercial decisions. Provided "as is" without warranty of any kind — see [LICENSE](LICENSE).

The implementation is based on the research copied into [`docs/RESEARCH.md`](docs/RESEARCH.md).

## What it answers

For a configured list of organisations, the tool can report:

- total minutes and hours by runner type across all orgs;
- per-org totals;
- pool and pipeline attribution where source data supports it;
- Microsoft-hosted vs GitHub-hosted Agents vs VMSS elastic pools vs Managed DevOps Pools vs self-hosted vs deployment-group pools;
- hosted image labels such as `ubuntu-latest`, `windows-2022`, and `macOS-14` when available from job requests;
- side-by-side reconciliation between requested-range Analytics hosted minutes and the current-period unsupported billing counter, with a variance percentage only when periods genuinely align;
- optional GitHub Actions cost estimates using user-supplied rates, leaving hosted minutes with unknown OS/image in an unpriced `unknown_hosted_os` bucket.

## Runner-type classification

Pools are discovered per organisation with:

```text
GET https://dev.azure.com/{org}/_apis/distributedtask/pools?api-version=7.1
```

Classification is deliberately pool-based, never build-default-queue-based:

| Decision | Runner bucket |
|---|---|
| `poolType == "deployment"` | `deployment_group` |
| `isHosted == true` and pool name is `GitHub-hosted Agents` | `github_hosted` |
| `isHosted == true` | `microsoft_hosted` |
| `options` contains `elasticPool` | `vmss_elastic_pool` |
| `agentCloudId` is not null | `managed_devops_pool` |
| otherwise | `self_hosted` |
| missing/deleted queue or pool metadata | `unknown` |

Pool IDs are **not deterministic across orgs**. The tool discovers pools and queues at run time. If a timeline record references a missing/deleted queue or pool, the minutes are reported as `unknown` and surfaced as unclassified minutes; the tool never assumes missing metadata means self-hosted. The deprecated `build.queue.pool.isHosted` field is not used for job attribution because multi-job and multi-stage runs can execute on different pools.

## Authentication: Microsoft Entra ID first, PAT fallback only by explicit opt-in

The default auth mode uses `azure-identity` `DefaultAzureCredential` and requests this Azure DevOps resource scope:

```text
499b84ac-1321-427f-aa17-267ca6975798/.default
```

The same token works for both:

- `https://dev.azure.com/{org}` REST APIs; and
- `https://analytics.dev.azure.com/{org}` OData APIs.

Tokens are cached in memory and refreshed before expiry so long-running timeline runs do not die at the one-hour token boundary. Tokens and secrets are never printed; logs redact authorization material.

### Human operator setup

1. Install the Azure CLI and sign in:

   ```bash
   az login
   ```

2. If an org is connected to a different tenant, sign in for that tenant and pass `--tenant <tenant-id>`:

   ```bash
   az login --tenant <tenant-id>
   ado-build-minutes doctor --tenant <tenant-id> --config config.toml
   ```

3. Ensure the signed-in user has the permissions listed below in every configured Azure DevOps org.

### Service principal / managed identity setup

`DefaultAzureCredential` also supports non-interactive identities:

- environment-variable service principals (`AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, plus a supported secret/certificate credential);
- managed identity;
- workload identity federation;
- Azure CLI credentials for local testing.

For Azure DevOps, a service principal or managed identity must be added to **each Azure DevOps organisation** as a user. Use the **Enterprise Application object ID**, not the app registration object ID. Grant **Basic** access and the relevant Azure DevOps permissions. Service principals cannot create PATs and do not have a user profile, so org discovery is intentionally config-driven.

### PAT fallback

PAT auth is disabled unless explicitly requested:

```bash
export AZURE_DEVOPS_EXT_PAT='...'
ado-build-minutes run --auth pat --config config.toml
```

The PAT is read only from `AZURE_DEVOPS_EXT_PAT`; there is no CLI token flag and the value is never logged.

## Permissions by data source

| Source | Required permissions | Notes |
|---|---|---|
| `analytics` / `ParallelPipelineJobsSnapshot` | Access to project Analytics, usually available to Basic users with project access | Cheapest headline numbers. |
| `analytics` / `TaskAgentRequestSnapshots` | **Project Collection Administrator** | Required for pool-consumption snapshot entities; 403 is reported per org/project and does not stop other orgs. |
| `jobrequests` | Agent pool read access; project/pipeline visibility improves attribution | Semi-documented endpoint; recent window only. |
| `timeline` | Build read access per project plus pool/queue read access | Authoritative but one timeline request per build. |
| `billing` | Organisation settings/build queue page access | Unsupported UI data-provider endpoint; current billing period only. |
| `doctor` | Same as the requested run sources | Source-aware probes: standard Analytics and PCA-gated TaskAgent Analytics are reported separately; timeline, jobrequests, and billing endpoints are probed only when requested. Large project/pool estates are sampled and labelled as such. |

## Installation

Python 3.11+ is required.

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e .
```

For a production collector environment, install the small runtime dependency set:

```bash
pip install -e '.[runtime]'
```

For tests:

```bash
pip install -e '.[runtime,dev]'
pip install pytest pytest-asyncio
pytest
```

## Configuration

Copy the example configuration and edit it for the estate:

```bash
cp config.example.toml config.toml
```

The example configuration uses placeholder organization names:

- `contoso-eng`
- `contoso-platform`
- `contoso-retail`
- `contoso-stores`
- `contoso-cloud`
- `contoso-data`
- `contoso-mobile`
- `contoso-security`
- `contoso-ops`
- `contoso-shared`
- `your-org-name`

Treat the org list as configuration: replace these placeholders with the Azure DevOps organizations in your estate by editing `orgs = [...]` in the config file.

## Quickstart

```bash
# 1. Check auth and permissions first
ado-build-minutes doctor --config config.toml --source analytics,jobrequests,billing

# 2. Fast headline run using Analytics OData (default source)
ado-build-minutes run --config config.toml --start 2026-08-01 --end 2026-08-31 --format all

# 3. Add recent hosted-image detail and current-period billing cross-check
ado-build-minutes run --config config.toml --source analytics,jobrequests,billing --start 2026-08-01 --end 2026-08-31 --format all

# 4. Expensive authoritative pass when historical deep attribution is needed
ado-build-minutes run --config config.toml --source timeline --start 2026-01-01 --end 2026-08-31 --out-dir ./out-timeline --concurrency 4
```

## Data source roles and trade-offs

Headline source selection is role-based, not a blind priority list:

- `analytics_parallel` is the default headline source. It covers the requested date range cheaply, but its runner split is hosted vs non-hosted only.
- `jobrequests` is enrichment/reconciliation data for recent per-job image/OS/pool detail. It is a headline source only when explicitly run by itself and only if every requested org is covered.
- `timeline` is an authoritative headline candidate only when coverage is complete; it is expensive and resumable.
- `analytics_taskagent_slots` provides pool-classified 10-minute slot/concurrency detail from `TaskAgentRequestSnapshots`; it is not a unique job count.
- `billing` is a current billing-period cross-check only and is never a requested-range headline.

A source must have complete per-org coverage and no source-level failures before it becomes the headline. If coverage is partial or mixed, the Markdown summary emits a prominent **MIXED PROVENANCE / INCOMPLETE COVERAGE** warning, lists source provenance by org, and intentionally does not show a clean combined total.

## Data source trade-offs

| Source | Fidelity | Cost | Retention / window | Runner type | Hosted image | When to use |
|---|---:|---:|---|---|---|---|
| `analytics` `ParallelPipelineJobsSnapshot` | Medium headline | Low | Analytics retention | Hosted vs non-hosted only | No | Default fast requested-range headline. |
| `analytics` `TaskAgentRequestSnapshots` | Job-slot-minutes, pool-level | Medium | ~30 days | PoolId classified via pool metadata | No | Pool consumption / concurrency view; PCA required; `MaxCount` is max concurrent slots per 10-minute interval, not unique jobs. |
| `jobrequests` | Per job | Low/medium | Undocumented recent window, commonly ~30 days | Pool ID | **Yes** | Recent image and pipeline breakdown. |
| `timeline` | Highest | Very high | Build retention | Queue → pool | No | Authoritative deep historical attribution. |
| `billing` | Hosted current-period counter | Low | Current billing period only | Hosted only | No | Ground-truth cross-check only; unsupported endpoint; variance percentage shown only if the billing period aligns with the requested range. |

## CLI reference

```text
ado-build-minutes --help
ado-build-minutes doctor --help
ado-build-minutes run --help
ado-build-minutes check --help
```

Global options are available on every subcommand:

| Flag | Default | Description |
|---|---|---|
| `--config PATH` | `config.toml` if present, otherwise built-in defaults | TOML config with orgs, concurrency, and optional Actions rates. |
| `--org ORG` | none | Add/override organisation; repeatable. If any `--org` is supplied, it is combined with config orgs. |
| `--orgs-file PATH` | none | Text file with one org per line. |
| `--tenant TENANT_ID` | none | Tenant override for token acquisition. |
| `--auth entra\|pat` | `entra` | Auth mode. PAT requires `AZURE_DEVOPS_EXT_PAT`. |
| `--concurrency N` | config or `6` | Bounded concurrent HTTP requests used by per-project, per-pool, and per-build collectors; hard-capped at `20`. |
| `--verbose` | false | Structured debug logging. |

`doctor` options:

| Flag | Default | Description |
|---|---|---|
| `--source analytics\|jobrequests\|timeline\|billing\|all` | `analytics` | Probe the real endpoints required by the sources a run will use. |

`run` options:

| Flag | Default | Description |
|---|---|---|
| `--source analytics\|jobrequests\|timeline\|billing\|all` | `analytics` | Comma-separated sources. |
| `--start YYYY-MM-DD` | 30 days before today | Inclusive UTC start date. |
| `--end YYYY-MM-DD` | today | Inclusive UTC end date. |
| `--format csv\|json\|markdown\|all` | `markdown` | Output format. |
| `--out-dir PATH` | `./out` | Output directory. |
| `--state-file PATH` | `.ado-build-minutes-state.json` | Timeline checkpoint file. |
| `--jobrequest-count N` | `1000` | Most recent completed requests per pool/agent to request. |
| `--actions-cost-model` | false | Apply user-supplied GitHub Actions rates from config. |

## Output files

Depending on `--format`, the tool writes:

- `detail.csv` / `detail.json` — one row per job where available and aggregate rows otherwise;
- `summary_by_org.csv`;
- `summary_by_org_runner_type.csv`;
- `summary_by_org_pool.csv`;
- `summary_by_org_runner_type_image.csv`;
- `summary_by_month_runner_type.csv`;
- `summary.json`;
- `executive_summary.md`.

Generated CSV/JSON/out/state files are ignored by git.

## Sample Markdown excerpt

```markdown
# Azure DevOps build minutes summary

Reporting window: 2026-08-01 to 2026-08-31
Primary source: analytics_parallel

| Runner type | Minutes | Hours | Jobs |
|---|---:|---:|---:|
| microsoft_hosted | 123,456.0 | 2,057.6 | 43,210 |
| self_hosted | 54,321.0 | 905.4 | 12,345 |

## Reconciliation

Analytics hosted minutes: 123,456.0
Billing current-period hosted minutes: 121,900.0
Periods not directly comparable: no variance percentage calculated
```

## Limitations and caveats

- Azure DevOps does not provide a native cross-org build-minute report because parallel-job capacity, not minutes, is the paid unit.
- `TaskAgentRequestSnapshots` and pool consumption data are effectively limited to about 30 days, require Project Collection Administrator, and report max concurrent slots per 10-minute interval rather than unique jobs.
- `jobrequests` is semi-documented/unsupported, has no server-side date filter, may be silently capped, and changes response shape when `$top` is used.
- `billing` uses an unsupported UI data-provider endpoint and can break without notice. It is current billing period only and is not directly comparable with arbitrary requested Analytics ranges unless the periods align.
- `timeline` is authoritative but expensive: there is no batch timeline endpoint, so each build requires a separate request.
- `build.queue.pool.isHosted` is deprecated and unreliable for multi-job runs; this tool classifies at job/pool level.
- Azure DevOps rate limits are enforced as 200 TSTU per five-minute sliding window per identity. The tool retries 429s, respects `Retry-After`, and slows down when rate-limit headers indicate low remaining budget.
- Hosted image breakdown is scalable only from `jobrequests`; timeline and Analytics headline records do not include the image label. Hosted minutes with unknown OS/image are placed in `unknown_hosted_os` and left unpriced rather than defaulting to Linux.
- Optional GitHub Actions cost estimates use editable, user-supplied rates. Verify all rates against current GitHub billing documentation before quoting. Unknown/deleted timeline queues are reported as `unknown` with unclassified minutes.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| 401 | Identity is not signed in, token tenant mismatch, or PAT missing/invalid | Run `az login`; use `--tenant`; for PAT set `AZURE_DEVOPS_EXT_PAT`. |
| 403 on projects/builds | Identity is not a member of the org/project or lacks Basic access | Add the user/SP/MI to the org and project. |
| 403 on `TaskAgentRequestSnapshots` | Missing Project Collection Administrator | Grant PCA or run without pool-consumption snapshot detail. |
| 403 on pools/jobrequests | Missing agent pool reader permissions | Grant Reader on organisation agent pools. |
| 429 / TF400733 | TSTU throttling | Lower `--concurrency`; wait for the five-minute window to reset. |
| Empty recent jobrequests | Endpoint retention/cap or no recent jobs | Use `analytics` for headline totals or `timeline` for retained builds. |
| Cross-tenant org fails | Org is backed by another Entra tenant | Acquire token with `--tenant <tenant-id>` and ensure the identity exists in that org. |

## How to answer a customer in 10 minutes

1. Confirm the org list in `config.toml`, adding every Azure DevOps organization in scope.
2. Run `ado-build-minutes doctor --config config.toml --source analytics,jobrequests,billing` and fix 401/403s before collecting.
3. Run:

   ```bash
   ado-build-minutes run --config config.toml --source analytics,billing --start <month-start> --end <today> --format all
   ```

4. Send `out/executive_summary.md`, calling out that Azure DevOps bills by parallel jobs, that Analytics is the headline source, and that the billing endpoint is an unsupported current-period cross-check.
5. If image-level GitHub Actions modelling is needed, run a second recent-window pass with `--source jobrequests --actions-cost-model`.

## License

This project is released under the MIT License. See [LICENSE](LICENSE).
