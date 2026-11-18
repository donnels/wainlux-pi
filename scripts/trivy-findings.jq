(["record", "severity", "identifier", "component", "installed_version", "fixed_version"] | @csv),
(.Results[]? | .Vulnerabilities[]?
  | ["vulnerability", .Severity, .VulnerabilityID, .PkgName, .InstalledVersion, .FixedVersion]
  | @csv),
(.Results[]? | .ExperimentalModifiedFindings[]?
  | select(.Type == "vulnerability" and .Status == "ignored")
  | .Finding
  | ["ignored", .Severity, .VulnerabilityID, .PkgName, .InstalledVersion, .FixedVersion]
  | @csv),
(.Results[]? | .Secrets[]?
  | ["secret", .Severity, .RuleID, .Category, "", ""]
  | @csv)
