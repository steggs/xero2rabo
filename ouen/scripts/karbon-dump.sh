#!/usr/bin/env bash
# Read-only dump of what Karbon holds, for the phase one audit.
#
# Run this on a machine with ordinary internet access. It makes GET requests
# only: it cannot change anything in Karbon. Output lands in ./karbon-dump as
# JSON files you can inspect, keep, or hand to someone to analyse.
#
# Credentials are read from the environment and are never written to the output
# or echoed. The files it produces contain client names and work items, so
# treat them as client data.
#
#   export KARBON_BEARER_TOKEN=...
#   export KARBON_ACCESS_KEY=...
#   ./scripts/karbon-dump.sh

set -euo pipefail

: "${KARBON_BEARER_TOKEN:?set KARBON_BEARER_TOKEN first}"
: "${KARBON_ACCESS_KEY:?set KARBON_ACCESS_KEY first}"

BASE="https://api.karbonhq.com"
OUT="${1:-./karbon-dump}"
mkdir -p "$OUT"

auth=(-H "Authorization: Bearer ${KARBON_BEARER_TOKEN}"
      -H "AccessKey: ${KARBON_ACCESS_KEY}"
      -H "Accept: application/json")

# Step one: does an authenticated call work at all.
echo "Checking access against ${BASE}/v3/TenantSettings"
code=$(curl -sS -o "$OUT/tenant-settings.json" -w '%{http_code}' \
       "${auth[@]}" "${BASE}/v3/TenantSettings" || true)

case "$code" in
  200)
    echo "  200 OK. API access works on this plan with these credentials."
    ;;
  401)
    echo "  401 Unauthorized."
    echo "  This does NOT by itself mean the plan excludes API access. The two"
    echo "  values are issued together and are not interchangeable, so the"
    echo "  commonest cause is the token and the access key being swapped."
    echo "  Check Settings, then Connected Apps in Karbon. If you can see or"
    echo "  create an API application there, the plan includes API access."
    echo "  If that section does not exist, that is the plan-level answer."
    exit 1
    ;;
  403)
    echo "  403 Forbidden. Authenticated, but this API application lacks"
    echo "  permission for the call. Widen its permissions in Karbon. This is a"
    echo "  scope problem, not a plan problem."
    exit 1
    ;;
  *)
    echo "  HTTP ${code}. Response body saved to $OUT/tenant-settings.json"
    exit 1
    ;;
esac

# Follow @odata.nextLink rather than incrementing $skip, as Karbon requires.
page_all () {
  local path="$1" name="$2" url="${BASE}${1}?\$top=100" page=0
  local tmp merged="$OUT/${name}.json"
  echo '{"value":[]}' > "$merged"
  while [ -n "$url" ]; do
    tmp="$OUT/.${name}.page.json"
    curl -sS -o "$tmp" "${auth[@]}" "$url"
    python3 - "$merged" "$tmp" <<'PY'
import json, sys
merged, page = sys.argv[1], sys.argv[2]
with open(merged) as f: acc = json.load(f)
with open(page) as f: new = json.load(f)
acc["value"].extend(new.get("value", []))
with open(merged, "w") as f: json.dump(acc, f, indent=2)
print(f"  {len(acc['value'])} records so far", file=sys.stderr)
PY
    url=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1])).get('@odata.nextLink',''))" "$tmp")
    page=$((page+1))
    [ "$page" -gt 200 ] && break
  done
  rm -f "$OUT/.${name}.page.json"
  echo "  wrote $merged"
}

echo "Pulling the client list"
page_all /v3/Organizations organizations
page_all /v3/Contacts contacts
page_all /v3/ClientGroups client-groups || echo "  (no client groups, or not enabled)"

echo "Pulling work templates and work items"
page_all /v3/WorkTemplates work-templates
page_all /v3/WorkItems work-items

echo "Pulling the work schedules behind recurring work"
python3 - "$OUT" <<'PY'
import json, os, subprocess, sys
out = sys.argv[1]
items = json.load(open(os.path.join(out, "work-items.json")))["value"]
keys = sorted({i["WorkScheduleKey"] for i in items if i.get("WorkScheduleKey")})
base = "https://api.karbonhq.com"
headers = [
    "-H", f"Authorization: Bearer {os.environ['KARBON_BEARER_TOKEN']}",
    "-H", f"AccessKey: {os.environ['KARBON_ACCESS_KEY']}",
    "-H", "Accept: application/json",
]
schedules = []
for key in keys:
    raw = subprocess.run(
        ["curl", "-sS", *headers, f"{base}/v3/WorkSchedules/{key}"],
        capture_output=True, text=True,
    ).stdout
    try:
        schedules.append(json.loads(raw))
    except json.JSONDecodeError:
        schedules.append({"WorkScheduleKey": key, "_error": raw[:200]})
path = os.path.join(out, "work-schedules.json")
json.dump({"value": schedules}, open(path, "w"), indent=2)
print(f"  wrote {path} ({len(schedules)} schedules)")
PY

echo
echo "Done. Files in $OUT:"
ls -la "$OUT"
echo
echo "Nothing in Karbon was modified: every call above was a GET."
