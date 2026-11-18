def active_vulnerabilities:
  [.Results[]? | .Vulnerabilities[]?];
def ignored_vulnerabilities:
  [.Results[]? | .ExperimentalModifiedFindings[]?
    | select(.Type == "vulnerability" and .Status == "ignored")
    | .Finding];
def secrets:
  [.Results[]? | .Secrets[]?];

[
  ["record", "metric", "value"],
  ["metadata", "image_id", $image_id],
  ["metadata", "scanned_utc", $scanned_utc],
  ["count", "vulnerability_critical", ([active_vulnerabilities[] | select(.Severity == "CRITICAL")] | length | tostring)],
  ["count", "vulnerability_high", ([active_vulnerabilities[] | select(.Severity == "HIGH")] | length | tostring)],
  ["count", "ignored_critical", ([ignored_vulnerabilities[] | select(.Severity == "CRITICAL")] | length | tostring)],
  ["count", "ignored_high", ([ignored_vulnerabilities[] | select(.Severity == "HIGH")] | length | tostring)],
  ["count", "secret_critical", ([secrets[] | select(.Severity == "CRITICAL")] | length | tostring)],
  ["count", "secret_high", ([secrets[] | select(.Severity == "HIGH")] | length | tostring)]
]
| .[]
| @csv
