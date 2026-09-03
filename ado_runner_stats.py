#!/usr/bin/env python3
"""
Azure DevOps – pipeline job stats broken down by runner (agent) type, across multiple organizations.

Runner types reported:
  MicrosoftHosted   – Microsoft-hosted pools (isHosted = true)
  ScaleSet          – Azure VM scale-set / elastic pools (isHosted = false, options contains "elasticPool")
  SelfHosted        – all other self-hosted pools
  Deployment        – deployment pools (poolType = deployment)

Data sources (all official, per org):
  1. Analytics OData  TaskAgentRequestSnapshots  -> one row per agent job request (jobs, minutes, queue time)
  2. REST  distributedtask/pools + /agents         -> pool inventory (agent counts, online/offline)

Auth: Microsoft Entra ID through Azure CLI. No PAT is used.
      Sign in first with one of:
        az login
        az login --identity
        az login --service-principal --username APP_ID --tenant TENANT_ID --certificate CERT_PATH

      The signed-in identity must be added to every Azure DevOps organization and
      have permission to view Analytics, agent pools, agents, and job requests.

Usage:
  python ado_runner_stats.py --orgs contoso fabrikam --days 30
  python ado_runner_stats.py --orgs-file orgs.txt --days 90 --out ./stats
  python ado_runner_stats.py --discover --days 30          # list all orgs the signed-in identity can access

Outputs (CSV) in --out folder:
  job_requests_raw.csv      every job request (org, project, pipeline, pool, runner type, durations)
  summary_by_runner.csv     org x runner type totals  (jobs, minutes, avg queue secs)
  summary_total.csv         runner type totals across all orgs
  pool_inventory.csv        every pool with agent counts and runner type
"""
import argparse, csv, json, os, subprocess, sys, time, urllib.error, urllib.parse, urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone

API = "7.1"
ANALYTICS_VER = "v4.0-preview"
AZURE_DEVOPS_RESOURCE = "499b84ac-1321-427f-aa17-267ca6975798"


# ---------- HTTP helpers ----------
class AzureCliTokenProvider:
    def __init__(self):
        self.token = None

    def get(self, force_refresh=False):
        if self.token and not force_refresh:
            return self.token

        command = [
            "az", "account", "get-access-token",
            "--resource", AZURE_DEVOPS_RESOURCE,
            "--query", "accessToken",
            "--output", "tsv",
        ]
        try:
            result = subprocess.run(command, check=True, capture_output=True, text=True)
        except FileNotFoundError:
            raise RuntimeError(
                "Azure CLI was not found. Install Azure CLI and run 'az login' first."
            ) from None
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout).strip()
            raise RuntimeError(
                "Could not get a Microsoft Entra token from Azure CLI. "
                f"Run 'az login' first. {detail}"
            ) from None

        self.token = result.stdout.strip()
        if not self.token:
            raise RuntimeError("Azure CLI returned an empty Microsoft Entra access token.")
        return self.token


def make_session():
    token_provider = AzureCliTokenProvider()
    token_provider.get()

    def get(url, retries=4):
        for attempt in range(retries):
            headers = {
                "Authorization": f"Bearer {token_provider.get()}",
                "Accept": "application/json",
            }
            req = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    return json.loads(r.read().decode())
            except urllib.error.HTTPError as e:
                if e.code == 401 and attempt < retries - 1:
                    token_provider.get(force_refresh=True)
                    continue
                if e.code in (429, 503) and attempt < retries - 1:
                    wait = int(e.headers.get("Retry-After", 2 ** (attempt + 1)))
                    time.sleep(wait)
                    continue
                body = e.read().decode(errors="ignore")[:300]
                raise RuntimeError(f"HTTP {e.code} for {url}\n{body}") from None
    return get


def odata_all(get, url):
    """Follow @odata.nextLink until exhausted."""
    while url:
        data = get(url)
        for row in data.get("value", []):
            yield row
        url = data.get("@odata.nextLink")


# ---------- classification ----------
def runner_type(pool: dict) -> str:
    if pool.get("poolType") == "deployment":
        return "Deployment"
    if pool.get("isHosted"):
        return "MicrosoftHosted"
    opts = str(pool.get("options") or "")
    if "elasticPool" in opts:
        return "ScaleSet"
    return "SelfHosted"


# ---------- per-org extraction ----------
def get_pools(get, org):
    url = f"https://dev.azure.com/{org}/_apis/distributedtask/pools?api-version={API}"
    pools = get(url).get("value", [])
    out = {}
    for p in pools:
        agents_url = f"https://dev.azure.com/{org}/_apis/distributedtask/pools/{p['id']}/agents?api-version={API}"
        try:
            agents = get(agents_url).get("value", [])
        except RuntimeError:
            agents = []  # hosted pools often deny agent listing; that's fine
        online = sum(1 for a in agents if a.get("status") == "online")
        out[p["id"]] = {
            "org": org,
            "poolId": p["id"],
            "poolName": p.get("name"),
            "runnerType": runner_type(p),
            "isHosted": p.get("isHosted"),
            "poolType": p.get("poolType"),
            "options": p.get("options"),
            "poolSize": p.get("size"),
            "agentsTotal": len(agents),
            "agentsOnline": online,
            "agentsOffline": len(agents) - online,
            "osDescriptions": ";".join(sorted({a.get("osDescription", "") for a in agents if a.get("osDescription")})),
        }
    return out


def get_job_requests(get, org, since: datetime):
    since_s = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    select = ("RequestId,PoolId,PipelineType,IsHosted,QueuedDate,StartedDate,FinishedDate,"
              "QueueDurationSeconds,IsQueued,IsRunning")
    query = (f"$select={select}&$expand=Project($select=ProjectName),Pipeline($select=PipelineName)"
             f"&$filter=QueuedDate ge {since_s}")
    url = (f"https://analytics.dev.azure.com/{org}/_odata/{ANALYTICS_VER}/TaskAgentRequestSnapshots?"
           + urllib.parse.quote(query, safe="=&$,()' :"))
    # Snapshots are sampled: dedupe on RequestId, keeping the row with the latest FinishedDate.
    best = {}
    for row in odata_all(get, url):
        rid = row.get("RequestId")
        if rid is None:
            continue
        if rid not in best or (row.get("FinishedDate") or "") > (best[rid].get("FinishedDate") or ""):
            best[rid] = row
    return list(best.values())


def parse_dt(s):
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


# ---------- org discovery ----------
def discover_orgs(get):
    me = get(f"https://app.vssps.visualstudio.com/_apis/profile/profiles/me?api-version={API}")
    accounts = get(f"https://app.vssps.visualstudio.com/_apis/accounts?memberId={me['id']}&api-version={API}")
    return sorted(a["accountName"] for a in accounts.get("value", []))


# ---------- main ----------
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--orgs", nargs="*", default=[], help="organization names (dev.azure.com/{org})")
    ap.add_argument("--orgs-file", help="text file, one org per line")
    ap.add_argument("--discover", action="store_true", help="auto-discover orgs the signed-in identity can access")
    ap.add_argument("--days", type=int, default=30, help="look-back window in days (default 30)")
    ap.add_argument("--out", default="./ado_runner_stats", help="output folder")
    args = ap.parse_args()

    try:
        get = make_session()
    except RuntimeError as exc:
        sys.exit(str(exc))

    orgs = list(args.orgs)
    if args.orgs_file:
        orgs += [l.strip() for l in open(args.orgs_file) if l.strip() and not l.startswith("#")]
    if args.discover:
        found = discover_orgs(get)
        print(f"Discovered {len(found)} org(s): {', '.join(found)}")
        orgs += found
    orgs = sorted(set(orgs))
    if not orgs:
        sys.exit("No organizations given. Use --orgs, --orgs-file or --discover.")

    since = datetime.now(timezone.utc) - timedelta(days=args.days)
    os.makedirs(args.out, exist_ok=True)

    raw_rows, inventory = [], []
    per_org = defaultdict(lambda: defaultdict(lambda: {"jobs": 0, "minutes": 0.0, "queueSecs": 0.0, "queued": 0}))

    for org in orgs:
        print(f"[{org}] pools ...", end="", flush=True)
        try:
            pools = get_pools(get, org)
        except RuntimeError as e:
            print(f" FAILED: {e.splitlines()[0]}")
            continue
        inventory += pools.values()
        print(f" {len(pools)} pools; job requests (last {args.days}d) ...", end="", flush=True)
        try:
            reqs = get_job_requests(get, org, since)
        except RuntimeError as e:
            print(f" FAILED: {e.splitlines()[0]}")
            continue
        print(f" {len(reqs)} jobs")

        for r in reqs:
            pool = pools.get(r.get("PoolId"))
            if pool:
                rtype = pool["runnerType"]
                pname = pool["poolName"]
            else:  # pool deleted or hidden – fall back to the Analytics flag
                rtype = "MicrosoftHosted" if r.get("IsHosted") else "SelfHosted"
                pname = f"(pool {r.get('PoolId')})"
            st, fn = parse_dt(r.get("StartedDate")), parse_dt(r.get("FinishedDate"))
            run_secs = (fn - st).total_seconds() if st and fn else None
            q_secs = r.get("QueueDurationSeconds")
            raw_rows.append({
                "org": org,
                "project": (r.get("Project") or {}).get("ProjectName"),
                "pipeline": (r.get("Pipeline") or {}).get("PipelineName"),
                "pipelineType": r.get("PipelineType"),
                "poolId": r.get("PoolId"),
                "poolName": pname,
                "runnerType": rtype,
                "requestId": r.get("RequestId"),
                "queuedDate": r.get("QueuedDate"),
                "startedDate": r.get("StartedDate"),
                "finishedDate": r.get("FinishedDate"),
                "queueSeconds": q_secs,
                "runSeconds": round(run_secs, 1) if run_secs is not None else None,
                "runMinutes": round(run_secs / 60, 2) if run_secs is not None else None,
            })
            agg = per_org[org][rtype]
            agg["jobs"] += 1
            agg["minutes"] += (run_secs or 0) / 60
            if q_secs is not None:
                agg["queueSecs"] += float(q_secs)
                agg["queued"] += 1

    # ---------- write outputs ----------
    def write(name, rows, fields):
        path = os.path.join(args.out, name)
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(rows)
        print(f"wrote {path} ({len(rows)} rows)")

    if raw_rows:
        write("job_requests_raw.csv", raw_rows, list(raw_rows[0].keys()))
    if inventory:
        write("pool_inventory.csv", inventory, list(inventory[0].keys()))

    summary, totals = [], defaultdict(lambda: {"jobs": 0, "minutes": 0.0, "queueSecs": 0.0, "queued": 0})
    for org, types in per_org.items():
        for rtype, a in types.items():
            summary.append({
                "org": org, "runnerType": rtype, "jobs": a["jobs"],
                "totalMinutes": round(a["minutes"], 1),
                "avgQueueSeconds": round(a["queueSecs"] / a["queued"], 1) if a["queued"] else None,
            })
            t = totals[rtype]
            t["jobs"] += a["jobs"]; t["minutes"] += a["minutes"]
            t["queueSecs"] += a["queueSecs"]; t["queued"] += a["queued"]
    summary.sort(key=lambda x: (x["org"], x["runnerType"]))
    write("summary_by_runner.csv", summary, ["org", "runnerType", "jobs", "totalMinutes", "avgQueueSeconds"])

    grand_jobs = sum(t["jobs"] for t in totals.values()) or 1
    total_rows = [{
        "runnerType": k, "jobs": t["jobs"], "jobsPct": round(100 * t["jobs"] / grand_jobs, 1),
        "totalMinutes": round(t["minutes"], 1),
        "avgQueueSeconds": round(t["queueSecs"] / t["queued"], 1) if t["queued"] else None,
    } for k, t in sorted(totals.items())]
    write("summary_total.csv", total_rows, ["runnerType", "jobs", "jobsPct", "totalMinutes", "avgQueueSeconds"])

    print("\n=== Totals across all orgs (last %d days) ===" % args.days)
    print(f"{'runnerType':<16}{'jobs':>10}{'%':>8}{'minutes':>14}{'avgQueue(s)':>14}")
    for r in total_rows:
        print(f"{r['runnerType']:<16}{r['jobs']:>10}{r['jobsPct']:>8}{r['totalMinutes']:>14}{str(r['avgQueueSeconds']):>14}")


if __name__ == "__main__":
    main()
