#!/usr/bin/env python3
from __future__ import annotations
# odf-storage-report.py v2.00
#
# ODF storage consumption breakdown for OpenShift clusters.
#
# Generates a read-only report covering:
#   - Ceph cluster health, pool usage, OSD distribution
#   - StorageCluster resource configuration
#   - ODF node CPU allocation
#   - PVC inventory across storage-related namespaces
#   - Per-org Quay registry breakdown (if Quay is deployed)
#   - ACM Observability spoke metrics storage (if ACM is deployed)
#
# Supports:
#   - Clusters with ODF only (no Quay, no ACM)
#   - Clusters without ODF (LVMS, SNO) — gracefully skips ODF sections
#   - Ceph tools pod optional — reports what it can without it
#   - RGW bucket-level breakdown via radosgw-admin (if ceph tools enabled)
#   - Quay 3.15 and 3.16 (auto-detected)
#   - ACM with MultiClusterObservability (auto-detected)
#
# Read-only: does NOT modify anything on the cluster.
#
# Usage:
#   export KUBECONFIG=/path/to/your-cluster-kubeconfig
#   ./odf-storage-report.py
#
# Requirements:
#   - oc (OpenShift CLI) in PATH
#   - python3 (3.7+)
#   - Ceph tools pod recommended (enableCephTools: true) but not required
#
# License: Apache-2.0
#
# Maintainers & Contributors:
#   Vincent Cojot & Johann Peyrard
#

import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

_IS_TTY = sys.stdout.isatty()

C_HDR = "\033[1;36m" if _IS_TTY else ""
C_OK  = "\033[1;32m" if _IS_TTY else ""
C_WRN = "\033[1;33m" if _IS_TTY else ""
C_ERR = "\033[1;31m" if _IS_TTY else ""
C_RST = "\033[0m"    if _IS_TTY else ""
C_DIM = "\033[2m"    if _IS_TTY else ""


def hdr(title: str) -> str:
    return f"\n{C_HDR}=== {title} ==={C_RST}"


def info(msg: str) -> str:
    return f"(II) {msg}"


def warn(msg: str) -> str:
    return f"{C_WRN}(WW) {msg}{C_RST}"


def err(msg: str) -> str:
    return f"{C_ERR}(EE) {msg}{C_RST}"


def pretty_bytes(b: int | float) -> str:
    b = float(b)
    if b >= 1024**4:
        return f"{b / 1024**4:.2f} TiB"
    if b >= 1024**3:
        return f"{b / 1024**3:.1f} GiB"
    if b >= 1024**2:
        return f"{b / 1024**2:.0f} MiB"
    if b >= 1024:
        return f"{b / 1024:.0f} KiB"
    return f"{int(b)} B"


SIZE_UNITS = {
    "B": 1, "Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4,
    "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "TiB": 1024**4,
}


def parse_k8s_size(s: str) -> int:
    """Parse Kubernetes-style sizes like '50Gi', '100Mi', '2Ti'."""
    s = s.strip()
    m = re.match(r"^(\d+(?:\.\d+)?)\s*(Ti|Gi|Mi|Ki|TiB|GiB|MiB|KiB|B)?$", s)
    if not m:
        return 0
    return int(float(m.group(1)) * SIZE_UNITS.get(m.group(2) or "B", 1))


# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------

def run(cmd: list[str], timeout: int = 60) -> str:
    """Run a command and return stdout. Returns '' on any failure."""
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
        return r.stdout if r.returncode == 0 else ""
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return ""


def run_json(cmd: list[str], timeout: int = 60):
    """Run a command and parse its stdout as JSON. Returns None on failure."""
    out = run(cmd, timeout=timeout)
    if not out:
        return None
    try:
        return json.loads(out)
    except (json.JSONDecodeError, ValueError):
        return None


class CephTools:
    """Wrapper around the rook-ceph-tools pod for Ceph CLI commands."""

    def __init__(self, pod_name: str):
        self.pod = pod_name

    def run(self, *args: str, timeout: int = 60) -> str:
        return run(
            ["oc", "exec", "-n", "openshift-storage", self.pod, "--"] +
            list(args),
            timeout=timeout,
        )

    def run_json(self, *args: str, timeout: int = 60):
        out = self.run(*args, timeout=timeout)
        if not out:
            return None
        try:
            return json.loads(out)
        except (json.JSONDecodeError, ValueError):
            return None


# ---------------------------------------------------------------------------
# Progress indicator
# ---------------------------------------------------------------------------

def progress(msg: str):
    """Overwrite in-place progress line on stderr (if tty)."""
    if sys.stderr.isatty():
        sys.stderr.write(f"\r{C_DIM}{msg}{C_RST}\033[K")
        sys.stderr.flush()


def progress_clear():
    if sys.stderr.isatty():
        sys.stderr.write("\r\033[K")
        sys.stderr.flush()


# ---------------------------------------------------------------------------
# Parallel execution helper
# ---------------------------------------------------------------------------

def gather(tasks: dict, max_workers: int = 10) -> dict:
    """Run {name: callable} in parallel, return {name: result}."""
    results = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(fn): name for name, fn in tasks.items()}
        for f in as_completed(futures):
            name = futures[f]
            try:
                results[name] = f.result()
            except Exception:
                results[name] = None
    return results


# ===================================================================
# Phase 1 — Pre-flight checks (sequential, fast)
# ===================================================================

def preflight():
    kubeconfig = os.environ.get("KUBECONFIG", "")
    if not kubeconfig:
        print(err("KUBECONFIG is not set."))
        print("    Export the hub cluster kubeconfig before running this script, e.g.:")
        print("      export KUBECONFIG=/path/to/kubeconfig")
        sys.exit(1)
    if not os.path.isfile(kubeconfig):
        print(err(f"KUBECONFIG={kubeconfig} does not exist."))
        sys.exit(1)

    hub_api = run(["oc", "whoami", "--show-server"]).strip()
    if not hub_api:
        print(err(f"Cannot reach cluster via KUBECONFIG={kubeconfig}"))
        print(err("Check your VPN, proxy, and certificate settings."))
        sys.exit(1)

    hub_fqdn = re.sub(r"https?://api\.", "", hub_api)
    hub_fqdn = re.sub(r":\d+/?$", "", hub_fqdn)
    hub_short = hub_fqdn.split(".")[0]

    return hub_api, hub_fqdn, hub_short


def discover_odf():
    """Discover ODF namespace, StorageCluster name, and ceph-tools pod."""
    has_odf = False
    has_sc = False
    sc_name = ""
    ceph: CephTools | None = None
    preflight_lines = []

    if run(["oc", "get", "namespace", "openshift-storage"]):
        has_odf = True
        sc_name = run([
            "oc", "get", "storagecluster", "-n", "openshift-storage",
            "-o", "jsonpath={.items[0].metadata.name}",
        ]).strip()
        if sc_name:
            has_sc = True

    if not has_odf:
        preflight_lines.append(info(
            "ODF not deployed (openshift-storage namespace not found)."))
        preflight_lines.append(info(
            "This cluster may use LVMS or another storage backend."))
    elif not has_sc:
        preflight_lines.append(info(
            "No ODF StorageCluster found. The openshift-storage namespace"))
        preflight_lines.append(info(
            "exists but may be used by other components (e.g. Observability)."))

    if has_sc:
        tools_pod = run([
            "oc", "get", "pods", "-n", "openshift-storage",
            "-l", "app=rook-ceph-tools",
            "-o", "jsonpath={.items[0].metadata.name}",
        ]).strip()
        if tools_pod:
            ceph = CephTools(tools_pod)
        else:
            preflight_lines.append(warn("No rook-ceph-tools pod found."))
            preflight_lines.append(warn(
                "Enable it for deeper Ceph and RGW bucket insights:"))
            preflight_lines.append(warn(
                f"  oc patch storagecluster {sc_name} -n openshift-storage"))
            preflight_lines.append(warn(
                "    --type merge -p "
                "'{\"spec\":{\"enableCephTools\":true}}'"))
            preflight_lines.append(warn("For more information:"))
            preflight_lines.append(warn(
                "  oc explain "
                "storageclusters.ocs.openshift.io.spec.enableCephTools"))

    return has_odf, has_sc, sc_name, ceph, preflight_lines


# ===================================================================
# Phase 2 — Parallel data gathering
# ===================================================================

def gather_ceph_data(ceph: CephTools | None) -> dict:
    if not ceph:
        return {}
    progress("Gathering data... [ceph]")

    def with_retry(fn, retries=1):
        """Retry a ceph call once if it returns empty (tools pod busy)."""
        result = fn()
        for _ in range(retries):
            if result is not None and result != "":
                break
            import time; time.sleep(2)
            result = fn()
        return result

    tasks = {
        "status":     lambda: with_retry(
            lambda: ceph.run("ceph", "status")),
        "df":         lambda: with_retry(
            lambda: ceph.run("ceph", "df")),
        "osd_tree":   lambda: with_retry(
            lambda: ceph.run("ceph", "osd", "df", "tree")),
        "pool_detail": lambda: with_retry(
            lambda: ceph.run_json(
                "ceph", "osd", "pool", "ls", "detail", "-f", "json")),
        "health":     lambda: with_retry(
            lambda: ceph.run("ceph", "health")),
        "osd_stat":   lambda: with_retry(
            lambda: ceph.run("ceph", "osd", "stat")),
    }
    return gather(tasks, max_workers=3)


def gather_odf_data(has_sc: bool, sc_name: str) -> dict:
    if not has_sc:
        return {}
    progress("Gathering data... [odf]")
    tasks = {
        "sc_json": lambda: run_json([
            "oc", "get", "storagecluster", sc_name,
            "-n", "openshift-storage", "-o", "json",
        ]),
        "nodes": lambda: run([
            "oc", "get", "nodes",
            "-l", "cluster.ocs.openshift.io/openshift-storage",
            "-o", "jsonpath={range .items[*]}{.metadata.name}{\"\\n\"}{end}",
        ]),
        "pvcs": lambda: run([
            "oc", "get", "pvc", "-n", "openshift-storage",
            "--no-headers", "-o", "custom-columns="
            "NAME:.metadata.name,"
            "CAPACITY:.status.capacity.storage,"
            "SC:.spec.storageClassName,"
            "STATUS:.status.phase",
        ]),
    }
    return gather(tasks)


def gather_node_cpu(node_name: str) -> tuple[str, str, str]:
    """Return (node_name, cpu_req_with_pct, cpu_lim_with_pct)."""
    out = run(["oc", "describe", "node", node_name])
    req = lim = "-"
    for line in out.splitlines():
        stripped = line.strip()
        if stripped.startswith("cpu") and "(" in stripped:
            parts = stripped.split()
            # Format: cpu  12197m (11%)  17160m (16%)
            if len(parts) >= 5:
                req = f"{parts[1]} {parts[2]}"
                lim = f"{parts[3]} {parts[4]}"
            elif len(parts) >= 3:
                req = f"{parts[1]} {parts[2]}"
            break
    return node_name, req, lim


def gather_rgw_data(ceph: CephTools | None) -> dict:
    if not ceph:
        return {}
    progress("Gathering data... [rgw]")
    zone_data = ceph.run_json("radosgw-admin", "zone", "list")
    rgw_zone = ""
    if zone_data:
        zones = [z for z in zone_data.get("zones", []) if z != "default"]
        if zones:
            rgw_zone = zones[0]
    if not rgw_zone:
        return {"zone": ""}

    tasks = {
        "bucket_stats": lambda: ceph.run_json(
            "radosgw-admin", "bucket", "stats",
            f"--rgw-zone={rgw_zone}"),
        "backingstore": lambda: run_json([
            "oc", "get", "backingstore", "-n", "openshift-storage",
            "-o", "json",
        ]),
        "obc_json": lambda: run_json([
            "oc", "get", "obc", "-A", "-o", "json",
        ]),
    }
    result = gather(tasks)
    result["zone"] = rgw_zone
    return result


def gather_obc_configmaps(obc_items: list) -> dict:
    """Fetch bucket names from OBC ConfigMaps in parallel."""
    if not obc_items:
        return {}

    def fetch_cm(ns, name):
        return run([
            "oc", "get", "configmap", name, "-n", ns,
            "-o", "jsonpath={.data.BUCKET_NAME}",
        ]).strip()

    tasks = {}
    for item in obc_items:
        ns = item["metadata"]["namespace"]
        nm = item["metadata"]["name"]
        tasks[f"{ns}/{nm}"] = lambda _ns=ns, _nm=nm: fetch_cm(_ns, _nm)

    return gather(tasks, max_workers=15)


def discover_quay_ns() -> str:
    for qns in ("quay", "quay-enterprise"):
        if run(["oc", "get", "namespace", qns]):
            return qns
    return ""


def gather_quay_data(quay_ns: str) -> dict:
    if not quay_ns:
        return {}
    progress("Gathering data... [quay]")
    tasks = {
        "pvcs": lambda: run([
            "oc", "get", "pvc", "-n", quay_ns,
            "--no-headers", "-o", "custom-columns="
            "NAME:.metadata.name,"
            "CAPACITY:.status.capacity.storage,"
            "SC:.spec.storageClassName,"
            "STATUS:.status.phase",
        ]),
    }

    # DB pod discovery — try both labels
    db_pod = ""
    for label in ("quay-component=postgres", "quay-component=quay-database"):
        candidate = run([
            "oc", "get", "pods", "-n", quay_ns, "-l", label,
            "-o", "jsonpath={.items[0].metadata.name}",
        ]).strip()
        if candidate:
            db_pod = candidate
            break

    quay_cr = run([
        "oc", "get", "quayregistry", "-n", quay_ns,
        "-o", "jsonpath={.items[0].metadata.name}",
    ]).strip()

    quay_ver = run([
        "oc", "get", "quayregistry", "-n", quay_ns,
        "-o", "jsonpath={.items[0].status.currentVersion}",
    ]).strip() or "unknown"

    result = gather(tasks)
    result["db_pod"] = db_pod
    result["quay_cr"] = quay_cr
    result["quay_ver"] = quay_ver
    result["quay_db"] = f"{quay_cr or 'regional-quay'}-quay-database"
    return result


def quay_psql(quay_ns: str, db_pod: str, quay_db: str, sql: str) -> str:
    return run([
        "oc", "exec", db_pod, "-n", quay_ns, "--",
        "psql", "-U", "postgres", "-d", quay_db,
        "-P", "pager=off", "-t", "-A", "-F|", "-c", sql,
    ])


def gather_quay_queries(quay_ns: str, db_pod: str, quay_db: str) -> dict:
    """Run Quay psql queries in parallel."""
    if not db_pod:
        return {}

    # Validate DB connectivity first
    check = run([
        "oc", "exec", db_pod, "-n", quay_ns, "--",
        "psql", "-U", "postgres", "-d", quay_db, "-c", "SELECT 1;",
    ])
    if not check:
        return {"error": f"Cannot query database {quay_db}"}

    tasks = {
        "quota_ns": lambda: quay_psql(quay_ns, db_pod, quay_db, """
            SELECT u.username,
                   pg_size_pretty(qns.size_bytes) AS consumed,
                   qns.size_bytes
            FROM quotanamespacesize qns
            JOIN "user" u ON qns.namespace_user_id = u.id
            WHERE qns.size_bytes > 0
            ORDER BY qns.size_bytes DESC;"""),
        "org_detail": lambda: quay_psql(quay_ns, db_pod, quay_db, """
            SELECT u.username,
                   count(DISTINCT r.id) AS repos,
                   count(DISTINCT m.id) AS manifests,
                   pg_size_pretty(coalesce(sum(DISTINCT s.image_size), 0)),
                   coalesce(sum(DISTINCT s.image_size), 0) AS raw_bytes
            FROM repository r
            JOIN "user" u ON r.namespace_user_id = u.id
            LEFT JOIN manifest m ON m.repository_id = r.id
            LEFT JOIN manifestblob mb ON mb.manifest_id = m.id
            LEFT JOIN imagestorage s ON s.id = mb.blob_id
            GROUP BY u.username
            ORDER BY coalesce(sum(DISTINCT s.image_size), 0) DESC;"""),
        "top10": lambda: quay_psql(quay_ns, db_pod, quay_db, """
            SELECT u.username || '/' || r.name AS fullname,
                   u.username AS org,
                   pg_size_pretty(coalesce(sum(DISTINCT s.image_size), 0)),
                   coalesce(sum(DISTINCT s.image_size), 0) AS raw_bytes
            FROM repository r
            JOIN "user" u ON r.namespace_user_id = u.id
            LEFT JOIN manifest m ON m.repository_id = r.id
            LEFT JOIN manifestblob mb ON mb.manifest_id = m.id
            LEFT JOIN imagestorage s ON s.id = mb.blob_id
            GROUP BY u.username, r.name
            ORDER BY coalesce(sum(DISTINCT s.image_size), 0) DESC
            LIMIT 10;"""),
        "total_bytes": lambda: quay_psql(quay_ns, db_pod, quay_db,
            "SELECT coalesce(sum(size_bytes),0) FROM quotanamespacesize;"
        ),
    }
    return gather(tasks)


def gather_pool_totals(ceph: CephTools | None, sc_name: str) -> dict:
    if not ceph:
        return {}
    rgw_pool = f"{sc_name}-cephobjectstore.rgw.buckets.data"
    block_pool = f"{sc_name}-cephblockpool"
    tasks = {
        "rados_rgw": lambda: ceph.run("rados", "df", "-p", rgw_pool),
        "rados_block": lambda: ceph.run("rados", "df", "-p", block_pool),
        "rgw_size": lambda: ceph.run(
            "ceph", "osd", "pool", "get", rgw_pool, "size"),
    }
    result = gather(tasks, max_workers=3)
    result["rgw_pool"] = rgw_pool
    result["block_pool"] = block_pool
    return result


def discover_acm() -> str:
    acm_ns = "open-cluster-management-observability"
    if run(["oc", "get", "namespace", acm_ns]):
        return acm_ns
    return ""


def gather_acm_data(acm_ns: str) -> dict:
    if not acm_ns:
        return {}
    progress("Gathering data... [acm]")
    tasks = {
        "managed": lambda: run([
            "oc", "get", "managedclusters", "-o", "json",
        ]),
        "pods": lambda: run_json([
            "oc", "get", "pods", "-n", acm_ns, "-o", "json",
        ]),
        "pvcs": lambda: run_json([
            "oc", "get", "pvc", "-n", acm_ns, "-o", "json",
        ]),
    }
    return gather(tasks)


def gather_acm_pvc_usage(
    acm_ns: str, pods_json: dict, pvcs_json: dict,
) -> list[dict]:
    """For each ACM PVC, find its mounting pod and get df usage in parallel."""
    if not pvcs_json:
        return []

    pod_by_pvc = {}
    for p in (pods_json or {}).get("items", []):
        pod_name = p.get("metadata", {}).get("name", "")
        for v in p.get("spec", {}).get("volumes", []):
            claim = v.get("persistentVolumeClaim", {}).get("claimName", "")
            if claim:
                pod_by_pvc[claim] = pod_name

    MOUNT_PATHS = [
        "/var/thanos/receive", "/var/thanos/compact",
        "/var/thanos/store", "/var/thanos/rule", "/alertmanager",
    ]

    def check_pvc(pvc_item):
        pvc_name = pvc_item["metadata"]["name"]
        pvc_size = (pvc_item.get("spec", {})
                    .get("resources", {})
                    .get("requests", {})
                    .get("storage", "?"))
        mount_pod = pod_by_pvc.get(pvc_name, "")
        used = pct = "?"
        if mount_pod:
            for mpath in MOUNT_PATHS:
                df_out = run([
                    "oc", "exec", mount_pod, "-n", acm_ns,
                    "--", "df", "-h", mpath,
                ])
                lines = df_out.strip().splitlines()
                if len(lines) >= 2:
                    parts = lines[-1].split()
                    if len(parts) >= 5:
                        used = parts[2]
                        pct = parts[4]
                        break
        return {"name": pvc_name, "alloc": pvc_size, "used": used, "pct": pct}

    tasks = {
        item["metadata"]["name"]: lambda _i=item: check_pvc(_i)
        for item in pvcs_json.get("items", [])
    }
    results = gather(tasks, max_workers=10)
    # Preserve original ordering
    return [
        results[item["metadata"]["name"]]
        for item in pvcs_json.get("items", [])
        if item["metadata"]["name"] in results
    ]


# ===================================================================
# Phase 3 — Rendering
# ===================================================================

def render_ceph_sections(ceph_data: dict, hub_short: str) -> list[str]:
    lines = []
    if not ceph_data:
        return lines

    NO_DATA = "  (ceph data not available — cluster may be busy)"

    lines.append(hdr(f"CEPH CLUSTER HEALTH \u2014 {hub_short}"))
    status = (ceph_data.get("status") or "").rstrip()
    lines.append(status if status else NO_DATA)

    lines.append(hdr("CEPH POOL USAGE"))
    df = (ceph_data.get("df") or "").rstrip()
    lines.append(df if df else NO_DATA)

    lines.append(hdr("OSD DISTRIBUTION & UTILISATION"))
    osd_tree = (ceph_data.get("osd_tree") or "").rstrip()
    lines.append(osd_tree if osd_tree else NO_DATA)

    lines.append(hdr("POOL PG DISTRIBUTION"))
    pools = ceph_data.get("pool_detail") or []
    if not pools:
        lines.append("  (no pool data available)")
    else:
        fmt = "  {:<60s}  {:>6}  {:>6}  {:>10}  {:>10}"
        lines.append(fmt.format("POOL", "PG_NUM", "PGP", "AUTOSCALE",
                                "CRUSH RULE"))
        lines.append("  " + "-" * 98)
        for p in pools:
            lines.append(fmt.format(
                p["pool_name"],
                p["pg_num"],
                p.get("pg_placement_num", p["pg_num"]),
                p.get("pg_autoscale_mode", "?"),
                str(p.get("crush_rule", "?")),
            ))

    return lines


def render_storagecluster(sc_json: dict | None) -> list[str]:
    lines = [hdr("STORAGECLUSTER RESOURCES")]
    if not sc_json:
        lines.append(info("No StorageCluster resource found."))
        return lines

    spec = sc_json.get("spec", {})
    phase = sc_json.get("status", {}).get("phase", "unknown")
    res = spec.get("resources", {})
    ds = (spec.get("storageDeviceSets") or [{}])[0]
    mcg = spec.get("multiCloudGateway", {}).get("endpoints", {})

    lines.append(f"  Phase:          {phase}")
    lines.append(
        f"  resourceProfile: {spec.get('resourceProfile', '(default)')}")
    lines.append(
        f"  deviceSet count: {ds.get('count', '?')}"
        f"  replica: {ds.get('replica', '?')}")
    lines.append("")

    fmt = "  {:<20s} {:>8s} / {:<8s}  {:>8s} / {:<8s}"
    lines.append(fmt.format(
        "COMPONENT", "CPU req", "limit", "MEM req", "limit"))
    lines.append("  " + "-" * 68)
    for name in ("mds", "mgr", "mon", "rgw", "noobaa-core",
                 "noobaa-db", "noobaa-endpoint"):
        r = res.get(name, {})
        req = r.get("requests", {})
        lim = r.get("limits", {})
        lines.append(fmt.format(
            name,
            str(req.get("cpu", "-")), str(lim.get("cpu", "-")),
            str(req.get("memory", "-")), str(lim.get("memory", "-")),
        ))

    osd_r = ds.get("resources", {})
    osd_req = osd_r.get("requests", {})
    osd_lim = osd_r.get("limits", {})
    lines.append(fmt.format(
        "osd (deviceSet)",
        str(osd_req.get("cpu", "-")), str(osd_lim.get("cpu", "-")),
        str(osd_req.get("memory", "-")), str(osd_lim.get("memory", "-")),
    ))

    if mcg:
        lines.append("")
        lines.append(
            f"  MCG endpoints:    "
            f"min={mcg.get('minCount', '?')} "
            f"max={mcg.get('maxCount', '?')}")

    return lines


def render_odf_nodes(node_cpus: list[tuple]) -> list[str]:
    lines = [hdr("ODF NODE CPU ALLOCATION")]
    if not node_cpus:
        lines.append(info(
            "No ODF-labelled nodes found "
            "(cluster.ocs.openshift.io/openshift-storage)."))
        return lines

    lines.append(
        f"  {'NODE':<45s}  {'CPU REQUESTS':>16s}  {'CPU LIMITS':>16s}")
    lines.append("  " + "-" * 80)
    for name, req, lim in node_cpus:
        lines.append(f"  {name:<45s}  {req:>16s}  {lim:>16s}")
    return lines


def render_pvc_inventory(title: str, pvc_text: str) -> list[str]:
    lines = [hdr(title)]
    if pvc_text and pvc_text.strip():
        pvc_lines = [l for l in pvc_text.strip().splitlines() if l.strip()]
        pvc_lines.sort(key=lambda l: parse_k8s_size(l.split()[1])
                       if len(l.split()) > 1 else 0)
        lines.extend(pvc_lines)
    else:
        lines.append(info("No PVCs found."))
    return lines


def render_rgw_buckets(
    ceph: CephTools | None,
    rgw_data: dict,
    obc_bucket_map: dict,
) -> list[str]:
    lines = [hdr("RADOSGW BUCKET USAGE")]

    if not ceph:
        lines.append(info(
            "Ceph tools pod not available. Enable it for bucket breakdown."))
        return lines

    zone = rgw_data.get("zone", "")
    if not zone:
        lines.append(info("No RGW zone detected. Skipping bucket breakdown."))
        return lines

    bucket_stats = rgw_data.get("bucket_stats") or []
    if not bucket_stats:
        lines.append("  (no buckets found in this zone)")
        return lines

    # Build NooBaa backing-store target set
    noobaa_targets = set()
    bs_data = rgw_data.get("backingstore") or {"items": []}
    for item in bs_data.get("items", []):
        tb = (item.get("spec", {})
              .get("s3Compatible", {})
              .get("targetBucket", ""))
        if tb:
            noobaa_targets.add(tb)

    # Build OBC consumer mapping
    obc_items = (rgw_data.get("obc_json") or {"items": []}).get("items", [])
    direct_map = {}
    noobaa_obcs = []
    for item in obc_items:
        ns = item["metadata"]["namespace"]
        nm = item["metadata"]["name"]
        sc = item.get("spec", {}).get("storageClassName", "")
        label = f"{ns}/{nm}"
        key = f"{ns}/{nm}"
        bkt = obc_bucket_map.get(key, "")
        if "noobaa" in sc:
            noobaa_obcs.append(label)
        elif bkt:
            direct_map[bkt] = label

    def identify(bucket_name, owner):
        if bucket_name in direct_map:
            return direct_map[bucket_name]
        if bucket_name in noobaa_targets:
            tag = "NooBaa backing store"
            if noobaa_obcs:
                tag += f" ({', '.join(noobaa_obcs)})"
            return tag
        if "prometheus" in owner or bucket_name.startswith("grafana-"):
            return "Grafana/Prometheus"
        return owner

    buckets = []
    for b in bucket_stats:
        name = b.get("bucket", "?")
        owner = b.get("owner", "?")
        usage = b.get("usage", {}).get("rgw.main", {})
        size = usage.get("size", 0)
        objs = usage.get("num_objects", 0)
        consumer = identify(name, owner)
        buckets.append((size, objs, consumer, name))

    buckets.sort(key=lambda x: x[0], reverse=True)

    w_name = max(max(len(t[3]) for t in buckets), 6)
    w_cons = max(max(len(t[2]) for t in buckets), 8)

    header = (f"  {'BUCKET':<{w_name}}  {'SIZE':>10}  "
              f"{'OBJECTS':>9}  {'CONSUMER':<{w_cons}}")
    lines.append(header)
    lines.append("  " + "-" * (len(header) - 2))

    total_size = total_objs = 0
    for size, objs, consumer, name in buckets:
        total_size += size
        total_objs += objs
        lines.append(
            f"  {name:<{w_name}}  {pretty_bytes(size):>10}  "
            f"{objs:>9}  {consumer:<{w_cons}}")

    lines.append("  " + "-" * (len(header) - 2))
    lines.append(
        f"  {'TOTAL':<{w_name}}  {pretty_bytes(total_size):>10}  "
        f"{total_objs:>9}  ({len(buckets)} buckets)")

    return lines


def render_obc(obc_items: list, obc_bucket_map: dict) -> list[str]:
    lines = [hdr("OBJECT BUCKET CLAIMS")]
    if not obc_items:
        lines.append(info("No ObjectBucketClaims found."))
        return lines

    lines.append(f"  {'NAMESPACE/OBC':<30s}  {'BUCKET':<45s}  STORAGE CLASS")
    lines.append("  " + "-" * 95)
    for item in obc_items:
        ns = item["metadata"]["namespace"]
        nm = item["metadata"]["name"]
        sc = item.get("spec", {}).get("storageClassName", "")
        bucket = obc_bucket_map.get(f"{ns}/{nm}", "?")
        lines.append(f"  {ns + '/' + nm:<30s}  {bucket:<45s}  {sc}")

    return lines


def render_quay(quay_ns: str, quay_data: dict, quay_q: dict) -> list[str]:
    lines = []
    if not quay_ns:
        lines.append(info("Quay namespace not found. Skipping Quay breakdown."))
        return lines

    lines.append(hdr(f"PVC INVENTORY ({quay_ns} namespace)"))
    pvc_text = quay_data.get("pvcs", "")
    if pvc_text and pvc_text.strip():
        lines.extend(pvc_text.strip().splitlines())
    else:
        lines.append(info("No PVCs found."))

    lines.append(hdr(f"QUAY \u2014 PER-ORG STORAGE BREAKDOWN ({quay_ns})"))

    db_pod = quay_data.get("db_pod", "")
    if not db_pod:
        lines.append(warn(
            f"No Quay database pod found in namespace '{quay_ns}'. "
            "Skipping Quay breakdown."))
        return lines

    quay_ver = quay_data.get("quay_ver", "unknown")
    quay_db = quay_data.get("quay_db", "")
    lines.append(info(
        f"Quay version: {quay_ver}  DB pod: {db_pod}  DB name: {quay_db}"))

    if quay_q.get("error"):
        lines.append(warn(quay_q["error"] + ". Skipping Quay breakdown."))
        return lines

    if not quay_q:
        lines.append(warn(
            f"Cannot query database {quay_db}. Skipping Quay breakdown."))
        return lines

    # Per-org storage
    lines.append("")
    lines.append("  Per-org storage (from quotanamespacesize):")
    lines.append("  " + "-" * 60)
    quota_ns = quay_q.get("quota_ns", "")
    for row in quota_ns.strip().splitlines():
        parts = row.split("|")
        if len(parts) >= 2:
            lines.append(f"  {parts[0]:<30s}  {parts[1]:>12s}")

    # Per-org detail
    lines.append("")
    lines.append("  Per-org detail (repos, manifests, blob storage):")
    lines.append("  " + "-" * 80)
    lines.append(
        f"  {'ORG':<20s}  {'REPOS':>6s}  {'MANIFESTS':>8s}  {'BLOB SIZE':>12s}")
    lines.append("  " + "-" * 80)
    org_detail = quay_q.get("org_detail", "")
    for row in org_detail.strip().splitlines():
        parts = row.split("|")
        if len(parts) >= 4:
            lines.append(
                f"  {parts[0]:<20s}  {parts[1]:>6s}"
                f"  {parts[2]:>8s}  {parts[3]:>12s}")

    # Top 10
    lines.append("")
    lines.append("  Top 10 largest repositories:")
    lines.append("  " + "-" * 100)
    lines.append(
        f"  {'REPOSITORY':<64s}  {'ORG':<15s}  {'SIZE':>12s}")
    lines.append("  " + "-" * 100)
    top10 = quay_q.get("top10", "")
    for row in top10.strip().splitlines():
        parts = row.split("|")
        if len(parts) >= 3:
            lines.append(
                f"  {parts[0]:<64s}  {parts[1]:<15s}  {parts[2]:>12s}")

    return lines


def render_pool_totals(
    pool_data: dict, quay_logical_bytes: int,
) -> list[str]:
    lines = [hdr("CEPH POOL TOTALS")]
    if not pool_data:
        lines.append(info(
            "Ceph tools pod not available. Enable it for pool totals."))
        return lines

    rgw_pool = pool_data.get("rgw_pool") or ""
    rados_rgw = pool_data.get("rados_rgw") or ""

    # Parse RGW pool size from rados df output
    rgw_bytes_str = rgw_unit = ""
    for line in rados_rgw.splitlines():
        if rgw_pool and rgw_pool in line:
            parts = line.split()
            if len(parts) >= 3:
                rgw_bytes_str = parts[1]
                rgw_unit = parts[2]
            break

    # Parse block pool usage
    block_pool = pool_data.get("block_pool") or ""
    rados_block = pool_data.get("rados_block") or ""
    block_used = "unknown"
    for line in rados_block.splitlines():
        if block_pool and block_pool in line:
            parts = line.split()
            if len(parts) >= 3:
                block_used = f"{parts[1]} {parts[2]}"
            break

    # Parse replica count
    rgw_size_out = pool_data.get("rgw_size") or ""
    replica = 3
    if rgw_size_out:
        parts = rgw_size_out.strip().split()
        if parts:
            try:
                replica = int(parts[-1])
            except ValueError:
                pass

    lines.append(
        f"  Ceph RGW pool (raw, {replica}x replicated):  "
        f"{rgw_bytes_str or '?'} {rgw_unit}")

    if quay_logical_bytes > 0 and rgw_bytes_str:
        try:
            rgw_raw = float(rgw_bytes_str) * SIZE_UNITS.get(rgw_unit, 1)
        except ValueError:
            rgw_raw = 0
        if rgw_raw > 0:
            quay_raw = quay_logical_bytes * replica
            other_raw = max(rgw_raw - quay_raw, 0)
            other_logical = other_raw / replica
            lines.append(
                f"  \u251c\u2500 Quay registry (logical):          "
                f"{pretty_bytes(quay_logical_bytes)}  "
                f"(~{pretty_bytes(quay_raw)} raw)")
            lines.append(
                f"  \u2514\u2500 Other (ACM metrics, etcd, etc.):  "
                f"~{pretty_bytes(other_logical)}  "
                f"(~{pretty_bytes(other_raw)} raw)")

    lines.append(
        f"  Ceph block pool (all RBD PVCs):      {block_used}")

    return lines


def render_acm(
    acm_ns: str, acm_data: dict, pvc_usage: list[dict],
) -> list[str]:
    lines = []
    if not acm_ns:
        lines.append(info(
            "ACM Observability not deployed. "
            "Skipping spoke metrics section."))
        return lines

    lines.append(hdr("ACM OBSERVABILITY \u2014 SPOKE CLUSTER METRICS STORAGE"))

    # Managed clusters
    managed_raw = acm_data.get("managed", "")
    managed = None
    if managed_raw:
        try:
            managed = json.loads(managed_raw)
        except (json.JSONDecodeError, ValueError):
            pass

    lines.append("  Managed clusters:")
    lines.append("  " + "-" * 80)
    lines.append(
        f"  {'CLUSTER':<35s}  {'AVAILABLE':<12s}"
        f"  {'JOINED':<12s}  AGE")
    lines.append("  " + "-" * 80)

    now = datetime.now(timezone.utc)
    for item in (managed or {}).get("items", []):
        name = item.get("metadata", {}).get("name", "?")
        created_str = item.get("metadata", {}).get("creationTimestamp", "")
        conditions = {
            c["type"]: c.get("status", "Unknown")
            for c in item.get("status", {}).get("conditions", [])
        }
        avail = conditions.get("ManagedClusterConditionAvailable", "Unknown")
        joined = conditions.get("ManagedClusterJoined", "Unknown")
        age = "?"
        if created_str:
            try:
                created = datetime.fromisoformat(
                    created_str.replace("Z", "+00:00"))
                age_days = (now - created).days
                age = f"{age_days}d"
            except ValueError:
                pass
        lines.append(f"  {name:<35s}  {avail:<12s}  {joined:<12s}  {age}")

    # PVC usage
    lines.append("")
    lines.append("  Observability PVC allocations and actual usage:")
    lines.append("  " + "-" * 80)
    lines.append(
        f"  {'PVC':<55s}  {'ALLOC':>6s}  {'USED':>6s}  {'USE%':>5s}")
    lines.append("  " + "-" * 80)

    totals: dict[str, list] = {}
    for pvc in pvc_usage:
        lines.append(
            f"  {pvc['name']:<55s}  {pvc['alloc']:>6s}"
            f"  {pvc['used']:>6s}  {pvc['pct']:>5s}")
        if "alertmanager" in pvc["name"]:
            grp = "Alertmanager"
        elif "thanos" in pvc["name"]:
            grp = "Thanos"
        else:
            grp = "Other"
        totals.setdefault(grp, [0, 0])
        totals[grp][0] += 1
        totals[grp][1] += parse_k8s_size(pvc["alloc"])

    lines.append("  " + "-" * 80)
    grand_count = sum(v[0] for v in totals.values())
    grand_bytes = sum(v[1] for v in totals.values())
    for grp in ("Alertmanager", "Thanos", "Other"):
        if grp in totals:
            cnt, b = totals[grp]
            lines.append(
                f"  {grp:<20s}  {cnt} PVCs  "
                f"allocated {pretty_bytes(b)}")
    lines.append(
        f"  {'TOTAL':<20s}  {grand_count} PVCs  "
        f"allocated {pretty_bytes(grand_bytes)}")

    return lines


def render_summary(
    hub_short: str,
    ceph: CephTools | None,
    ceph_data: dict,
    has_sc: bool,
    sc_json: dict | None,
    quay_logical_bytes: int,
) -> list[str]:
    lines = [hdr("SUMMARY")]
    lines.append(f"  Cluster:          {hub_short}")

    sc_count = "?"
    if sc_json:
        try:
            sc_count = str(
                sc_json["spec"]["storageDeviceSets"][0]["count"])
        except (KeyError, IndexError, TypeError):
            pass

    if ceph and ceph_data:
        health_out = ceph_data.get("health") or ""
        health = health_out.split()[0] if health_out.strip() else "?"
        df_out = ceph_data.get("df") or ""
        total_raw = total_avail = total_used = pct = "?"
        for line in df_out.splitlines():
            if "TOTAL" in line:
                parts = line.split()
                if len(parts) >= 8:
                    total_raw = f"{parts[1]} {parts[2]}"
                    total_avail = f"{parts[3]} {parts[4]}"
                    total_used = f"{parts[5]} {parts[6]}"
                    pct = parts[-1]
                break

        osd_stat = ceph_data.get("osd_stat") or ""
        osd_count = osd_stat.split()[0] if osd_stat.strip() else "?"

        lines.append(f"  Health:           {health}")
        lines.append(
            f"  OSDs:             {osd_count} "
            f"(count={sc_count} x replica=3)")
        lines.append(f"  Raw capacity:     {total_raw}")
        lines.append(f"  Used:             {total_used} ({pct}%)")
        lines.append(f"  Available:        {total_avail}")
    elif has_sc:
        lines.append(
            "  Ceph tools:       not available "
            "(enable for capacity details)")
    else:
        lines.append("  ODF:              not deployed")

    if quay_logical_bytes > 0:
        lines.append(f"  Quay total:       {pretty_bytes(quay_logical_bytes)}")

    lines.append("")
    lines.append(info(
        f"Report generated at "
        f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}"))

    return lines


# ===================================================================
# Main
# ===================================================================

def main():
    # Phase 1: pre-flight
    hub_api, hub_fqdn, hub_short = preflight()

    output = []
    print(info(f"Connected to: {hub_short} ({hub_api})"))

    has_odf, has_sc, sc_name, ceph, preflight_lines = discover_odf()
    for line in preflight_lines:
        print(line)

    # Phase 2: parallel data gathering
    progress("Gathering data...")

    # Wave 1: independent discovery + ceph + odf (all parallel)
    wave1 = {}
    wave1["ceph"] = lambda: gather_ceph_data(ceph)
    wave1["odf"] = lambda: gather_odf_data(has_sc, sc_name)
    wave1["quay_ns"] = lambda: discover_quay_ns()
    wave1["acm_ns"] = lambda: discover_acm()
    if ceph:
        wave1["rgw"] = lambda: gather_rgw_data(ceph)

    w1 = gather(wave1)
    ceph_data = w1.get("ceph", {})
    odf_data = w1.get("odf", {})
    quay_ns = w1.get("quay_ns", "")
    acm_ns = w1.get("acm_ns", "")
    rgw_data = w1.get("rgw", {})

    # Wave 2: things that depend on wave 1 results
    wave2 = {}

    # Node CPU — need node list from wave 1
    node_names = []
    nodes_raw = odf_data.get("nodes", "")
    if nodes_raw:
        node_names = [n.strip() for n in nodes_raw.strip().splitlines()
                      if n.strip()]

    # OBC configmaps — need obc_json from rgw_data
    obc_items = []
    if rgw_data:
        obc_items = (rgw_data.get("obc_json") or {}).get("items", [])
    # Also try standalone fetch if no RGW data but OBCs may exist
    if not obc_items and has_sc:
        standalone_obc = run_json(["oc", "get", "obc", "-A", "-o", "json"])
        if standalone_obc:
            obc_items = standalone_obc.get("items", [])

    wave2["quay_data"] = lambda: gather_quay_data(quay_ns)
    wave2["acm_data"] = lambda: gather_acm_data(acm_ns)
    wave2["obc_cms"] = lambda: gather_obc_configmaps(obc_items)

    # Node CPU in parallel
    if node_names:
        for n in node_names:
            wave2[f"node_{n}"] = lambda _n=n: gather_node_cpu(_n)

    w2 = gather(wave2)

    quay_data = w2.get("quay_data", {})
    acm_data = w2.get("acm_data", {})
    obc_bucket_map = w2.get("obc_cms", {})

    node_cpus = []
    for n in node_names:
        val = w2.get(f"node_{n}")
        if val:
            node_cpus.append(val)

    # Wave 3: things that depend on wave 2 (quay queries, acm pvc usage,
    #          pool totals)
    wave3 = {}
    db_pod = quay_data.get("db_pod", "")
    quay_db = quay_data.get("quay_db", "")

    wave3["quay_q"] = lambda: gather_quay_queries(
        quay_ns, db_pod, quay_db) if db_pod else {}

    if acm_ns and acm_data:
        wave3["acm_pvc"] = lambda: gather_acm_pvc_usage(
            acm_ns,
            acm_data.get("pods", {}),
            acm_data.get("pvcs", {}),
        )

    if ceph and has_sc:
        wave3["pool_totals"] = lambda: gather_pool_totals(ceph, sc_name)

    w3 = gather(wave3)

    quay_q = w3.get("quay_q", {})
    acm_pvc_usage = w3.get("acm_pvc", [])
    pool_data = w3.get("pool_totals", {})

    # Parse Quay total bytes for pool totals and summary
    quay_logical_bytes = 0
    if quay_q:
        total_str = quay_q.get("total_bytes", "").strip()
        try:
            quay_logical_bytes = int(total_str)
        except (ValueError, TypeError):
            pass

    progress_clear()

    # Phase 3: render everything
    output.extend(render_ceph_sections(ceph_data, hub_short))

    if has_sc:
        output.extend(render_storagecluster(odf_data.get("sc_json")))
        output.extend(render_odf_nodes(node_cpus))
        output.extend(render_pvc_inventory(
            "PVC INVENTORY (openshift-storage)",
            odf_data.get("pvcs", "")))
        output.extend(render_rgw_buckets(ceph, rgw_data, obc_bucket_map))
        output.extend(render_obc(obc_items, obc_bucket_map))
        output.extend(render_quay(quay_ns, quay_data, quay_q))
        output.extend(render_pool_totals(pool_data, quay_logical_bytes))

    output.extend(render_acm(acm_ns, acm_data, acm_pvc_usage))
    output.extend(render_summary(
        hub_short, ceph, ceph_data, has_sc,
        odf_data.get("sc_json"), quay_logical_bytes))

    # Print all output at once
    print("\n".join(output))


if __name__ == "__main__":
    main()
