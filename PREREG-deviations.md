# PREREG deviations
All entries were written on 2026-11-07, after the offline baseline
(reports/prereg_baseline.md, commit 137f749) and BEFORE any live registry check (T0).
No registration outcome had been seen.

D0. Section 0 confirmation: the universal list was sent privately only to the registries
    during coordinated disclosure: PyPI Security (PyPI part, 2026-04-29) and Socket
    (npm part, 2026-04-29). As far as the author knows, nobody else: the June 2026
    academic access request got no reply, and the August 2026 request was answered
    with a link to the then-public tag v0.2-preprint.
D1. py_false_positive_packages.csv is not an import-to-distribution list. It is the
    Spracklen et al. list of regex false positives and matches 0 of our names.
    Import names that differ from distribution names are handled by the pipreqs
    mapping (commit 48dbafd39003b9de177b8d314795e45797555850, file sha256 bc02385c473b7ccfdc2c67697df423feaa394023f5f1d59693c86a5a0dcd7e50). A PyPI name in this mapping whose
    mapped distribution EXISTS is not a hallucination. Removed counts are reported.
D2. For batch records, the batch completion time is an upper bound and the batch
    submission time (batches.submitted_at) is the lower bound. If a package's creation
    date falls inside this interval, its as-of-generation status is UNKNOWN.
D3. Added sensitivity analysis (primary test unchanged): Cochran-Mantel-Haenszel
    test stratified by ecosystem x frequency tertile x length tertile (13 common
    strata), plus a balance table of A vs B4 per stratum.
D4. Clarification: inside common strata npm has A = 18 and B4 = 6 (minimum
    detectable effect above 60%), so npm-only results are descriptive.
D5. Clarification: the universal set has 139 names in the database before
    exclusions and 138 after the noise filter (one PyPI extraction artifact).
D6. Clarification: the generations table has 199,840 rows, the paper reports
    199,845 (39,969 prompts x 5 models). Task 1 rates use the stored rows; the gap
    of 5 is explained in the v3 -> new diff report.
D7. Registrability is known only for A: the registries reviewed the universal set
    in April-May 2026, while B was never reviewed. The primary analysis is unchanged
    (all names stay in the denominators). Sensitivity: drop from A the names a registry
    explicitly said cannot be registered by outsiders (the four @ember/* scoped names,
    and PyPI names that PyPI marked as blocked, if they can be identified from its reply),
    and report npm scoped names in every group separately.
D8. A1 frozen: 18 names (sha256 in notes/a1-final.sha256). First appearance:
    10 names in v1, 8 names in v2.
D9. Correction to D5: the database has 154 universal names without any filter
    (121 PyPI + 33 npm), 139 under the v3 counting (121 PyPI + 18 npm after the
    old SQL filter), and 138 after the current noise filter (one PyPI name fewer
    than the v3 counting; npm unchanged at 18).
D10. Clarification to D1: before T0 only the number of names that match the
    pipreqs mapping is reported (per group, from the offline baseline). The rule
    itself (mapped distribution EXISTS) is applied after the live checks, and the
    names it removes are counted then.
D11. Refinement of D1. The pipreqs rule removes a name only if every extraction
     of that name comes from an import statement. A name that appears at least once
     as an install target (pip install, requirements, uv add and similar) stays a
     candidate hallucination even if the mapping points to an existing distribution,
     because installing it fetches whatever is registered under that exact name.
     Decided before T0 on conceptual grounds; offline counts are in
     reports/prereg_baseline.md (commit 9cc8b599498f08a562aa794914d3b50428939534).
D12. Clarification to D11 and a limitation stated before T0. In the data, an install
     target is an H1 extraction: for Python, `pip install X` anywhere in the response
     (first argument only); for JavaScript, `npm install X` inside code blocks. Other
     install forms (requirements files, uv add, poetry add, yarn add, pnpm add) were not
     extracted. The pipreqs mapping covers about 1,157 common import names, so rare
     import-name/distribution-name mismatches are not caught. This mainly affects B
     (the long tail) and is reported as a limitation.

## Written after T0: 2026-11-07, after the round 1 aggregates and before round 2.
## No breakdown by group A/B had been seen.

D13. npm special statuses come from the npm registry document and override the
     two-method rule: a package with a time.unpublished field (UNPUBLISHED) or with
     0.0.1-security as its only version (SECURITY_HOLDING) keeps that status even if
     deps.dev does not list it. Both count as registered, as in PREREG section 4.2.
D14. A PyPI name for which the Simple API says EXISTS and the JSON API says ABSENT in
     both rounds is reported as EXISTS_SIMPLE_ONLY (for example, a project without
     files). The primary analysis treats it as UNKNOWN, as pre-registered.
     Sensitivity analysis: count it as registered.
D15. Names that are not valid package names under the registry's own naming rules
     (PyPI: PEP 508 name syntax; npm: validate-npm-package-name rules for new
     packages), including names with whitespace or line breaks, are extraction noise
     and are excluded. The number removed in each group is reported.

D17. Written after the npm placeholder check and before any A/B breakdown. An npm name
     whose package was unpublished (time.unpublished) before the name's generation time
     was not installable at generation time. Stage A counts it as a false positive
     (created before generation). Sensitivity for Task 1 and Task 2: count such names as
     hallucinations as of generation (not registered unless republished later). The
     primary analysis is unchanged.

D16. Drafted with the stage B plan; committed after D17 and before any A/B breakdown.
     REMOVED_MALICIOUS names have no creation date. In the primary Task 2 analysis they
     are excluded as UNKNOWN, as in stage A. Sensitivity: a REMOVED_MALICIOUS name whose
     OSV MAL record was published after the name's generation time counts as registered
     after generation; one published before counts as pre-existing.
D18. arXiv shows submission times for each version, not announcement times. The A1
     exposure start is the submission time of the first version that prints the name
     (from the arXiv submission history), used as a proxy for the announcement date.
     The difference is at most a few days.
