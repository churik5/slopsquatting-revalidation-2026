# Pre-registration: Task 1 (revalidation) and Task 2 (registration dynamics)

Author: Aleksandr Churilov
Written: 2026-11-07. The git commit time of this file is its registration time.

This plan is written **before** the live registry checks for this study are run and
**before** any registration outcome for names outside the universal set is looked at.
Everything the author already knew is listed in section 0. Any later change goes to
`PREREG-deviations.md` with a date and a reason. Nothing in this file is edited after
the first live check starts.

---

## 0. Known before registration

- **Paper v3 numbers.** 199,845 generations; per-model hallucination rates 4.62–6.10%;
  universal set reported as 127 names (109 PyPI + 18 npm); 53 registrable (41 PyPI + 12 npm)
  according to the registries' own review in April–May 2026. The database gives
  121 PyPI names, not 109, so the true universal set in the DB is 139 names.
- **Partial outcome for the universal set (July 2026).** All 139 names were checked by hand
  in July 2026. Six names exist in the registries (4 PyPI, 2 npm), and all six were created
  **before** the April 2026 runs, so they are false positives of the old master list, not
  registrations. As of 2026-07-23, no name in the universal set was seen registered after
  the runs.
- **Registry reviews (April–May 2026).** PyPI said that 68 of the 109 names are blocked by its
  prohibited-name list and ultranormalization. Socket said that 4 of the 18 npm names are
  framework virtual modules, 1 is a published package, and 1 is a security holding package.
- **Exposure of the universal list** (details in `notes/exposure-record-2026-11-07.md`):
  public in the GitHub repository from 2026-04-30; open Zenodo v0.2 archive from 2026-04-30
  (20 downloads); a public fork created 2026-07-26 that still has the list. The author's own
  copies were removed or restricted on 2026-11-07.
- **The database (runs.db) was never shared:** no Zenodo deposit, no access requests, and the
  July 23 email to Socket says it was not attached.
- **Spracklen et al.** do not publish their list of hallucinated names. Their public artifact
  (Zenodo 10.5281/zenodo.14676377, GitHub Spracks/PackageHallucination) has only aggregate
  plot data (checked 2026-11-07).
- No registration outcome has been looked at for any name outside the universal set.

Author confirms before commit (edit this line, then never again):
- [ ] The universal list was sent privately to: ______ (or "nobody") on ______.

---

## 1. Data

- Source: `data/runs.db`, opened read-only. All work runs on a copy.
- Name universe: every package name extracted from the generations (the `validations`
  keyset), per model and per ecosystem.
- Generation time: per-generation timestamp from the database. If a record has none, the
  completion time of its batch is used, and the report says how many records needed this.
- Freeze: before any network check, a script writes the SHA-256 of the sorted, normalized
  name list for each ecosystem. Only the hashes are committed, never the names.

## 2. Definitions

**Normalization.** PyPI: PEP 503. npm: lowercase for comparison, scope kept,
`/` in scoped names URL-encoded for requests.

**State of one check:**
- `EXISTS`: HTTP 200 with a valid project or package document.
- `ABSENT`: HTTP 404.
- `UNKNOWN`: 429, 5xx, timeout, network error, a body that cannot be parsed, or a
  redirect that cannot be resolved.

**Methods.** PyPI: JSON API and Simple API. npm: the registry document and one independent
source (deps.dev). The exact endpoints are recorded in the code. Every response goes into an
append-only table `registry_checks` (name, ecosystem, method, url, http_status, state,
body_sha256, checked_at_utc). Nothing is ever updated or deleted in this table.

**Final state of a name:**
- `EXISTS` if both methods say EXISTS.
- `ABSENT` only if both methods say ABSENT in two rounds at least 12 hours apart.
- `UNKNOWN` in every other case. UNKNOWN is reported separately and is never merged
  into ABSENT.

**Special statuses** (reported as separate categories):
- `SECURITY_HOLDING`: npm package whose only version is `0.0.1-security`.
- `UNPUBLISHED`: npm document with a `time.unpublished` field.
- `REMOVED_MALICIOUS`: listed in OSV / OpenSSF malicious-packages.
- `BLOCKED_BY_REGISTRY`: only when the registry itself has said so (for example,
  PyPI's April 2026 review).

**Creation date.** npm: `time.created`. PyPI: the earliest upload time across all files of all
releases. This is the earliest *visible* upload. If files were deleted, the true date can be
earlier, so such cases are flagged.

**Hallucination (as of generation).** An extracted name that did not exist as a package at the
time of its generation. In practice:
- final `ABSENT` now → hallucination;
- `EXISTS` now with creation date after the generation time → hallucination that was
  registered later;
- `EXISTS` now with creation date before the generation time → false positive (excluded);
- `EXISTS` now with unknown creation date → `UNKNOWN`.

**Not hallucinations (excluded):**
- names removed by the noise filter (`src/slop_bench/noise_filter.py`; its git hash is
  recorded in the report);
- Python standard library modules: union of `sys.stdlib_module_names` for Python 3.10–3.13;
- Node built-in modules: union of `require('module').builtinModules` for Node 18, 20, 22,
  plus any name with the `node:` prefix;
- import names covered by the existing import-to-distribution list
  (`py_false_positive_packages.csv`).

Each exclusion rule reports how many names it removed.

## 3. Task 1: what is reported

1. Per-model hallucination rate at the generation level (a generation counts if it has at
   least one hallucinated name), with 95% Wilson intervals. It is computed three ways:
   (a) the v3 method, (b) v3 minus stdlib/built-in names, (c) the live revalidation above.
2. Universal set: names hallucinated by all five models after revalidation, per ecosystem.
3. A table "v3 value → new value → reason" for every number in the paper.
4. A table of status changes (v3 label → new state), with counts only and no names.

All numbers are written by one script into `reports/numbers.json`. No number is typed by hand.
If any paper number changes, arXiv v4 is posted with the full diff, whichever direction
the change goes.

## 4. Task 2: registration dynamics

### 4.1 Groups

All groups contain only names that were hallucinations as of generation (section 2).

- **A1, printed in the paper:** names that appear in the text or tables of any arXiv version
  of the preprint. Exposure starts on the first arXiv announcement date that shows the name
  (taken from the arXiv listing by script).
- **A2, list only:** names of the universal set that are in the disclosure files but not
  printed in the paper. Exposure starts 2026-04-30. The author's copies ended 2026-11-07,
  but a public fork has kept a copy since 2026-07-26, so exposure is treated as ongoing.
- **B, never published:** all other hallucinated names in runs.db.

### 4.2 Outcome

- **Registered** = final `EXISTS` (including `SECURITY_HOLDING`) with creation date on or
  after the name's generation time.
- **Primary outcome:** registered between the name's exposure start (for A) or 2026-04-30
  (for B) and the analysis date T0.
- **T0** = the date the first live-check run starts. It is written to `numbers.json`
  automatically.
- Security holding and removed-malicious cases are counted inside "registered" and are also
  shown separately.

### 4.3 Hypothesis

**H1.** The share of names registered after 2026-04-30 is higher in A (A1 ∪ A2) than in matched
B names.

**Known confound.** A is made only of names that all five models produced, which are the most
"natural" names. B contains no five-model names by construction, so exact matching on the
number of models is impossible.

- **Primary comparison:** A vs B names produced by exactly four models, matched on ecosystem
  (exact), on total frequency in the corpus (tertiles) and on name length (tertiles).
- **Secondary, descriptive only:**
  - A vs all of B;
  - A1 vs A2 (strength of exposure);
  - time from exposure start to creation for registered names.

### 4.4 Analysis

- **Primary test:** Fisher's exact test, two-sided, alpha = 0.05, on registered / not
  registered, A vs matched B. The difference is reported with a 95% Newcombe interval and each
  share with a 95% Wilson interval.
- **Sensitivity:** the same test per ecosystem, and a Cochran–Mantel–Haenszel test stratified
  by ecosystem.
- **Minimum detectable effect:** computed **before T0** from the database counts of each group
  (no network needed), for baseline rates of 1%, 2% and 5%, with power 0.8. It is written to
  `numbers.json` and committed before the live checks start.
- A null result is reported as "no effect detected at this precision", together with the
  minimum detectable effect.
- **No attribution of intent.** Registered names are described only by categories taken from
  registry metadata: security holding, removed malicious, project with code, empty
  placeholder, unknown. Packages are never installed or run. Registrants' identities are not
  recorded.

### 4.5 Exclusions and missing data

The following are excluded from the primary denominators and reported as counts:
- final `UNKNOWN` states;
- names without a generation time;
- names whose PyPI creation date is flagged as possibly late.

The primary test is re-run with the flagged PyPI names included as a sensitivity check.

## 5. Ethics

- No name is registered, not even defensively.
- No third-party package is installed or executed. Only registry metadata is read.
- Free names that are referenced anywhere real are reported privately to PyPI Security,
  npm/GitHub and Socket.
- Papers and artifacts contain aggregates, hashes, and names that are already closed.
- Lists of names are stored only in this private repository. A pre-commit hook blocks
  name-list files.

## 6. Limitations stated in advance

- A is small (about 130 names), so only large effects can be detected.
- The strength of exposure is only partly known: there are Zenodo download counts and one
  fork, but no download counts from GitHub.
- PyPI creation dates can be late when files were deleted.
- deps.dev may lag behind the registries, which can produce UNKNOWN states for new packages.
- Names printed as examples in other papers (including Spracklen et al.) are not checked.
