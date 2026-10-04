# Security

This document records how ResearchSphere AI handles **dependency advisories** as
the repository stands today: which audits block a build, which only report, and
every advisory that has been explicitly accepted rather than fixed.

It exists because `.github/workflows/security.yml` refers to it. An accepted
advisory is only defensible if the reasoning is written down somewhere a
reviewer can find it, and the workflow is deliberately wired so that an
exception cannot be added without a corresponding section here.

**Vulnerability reporting is not yet defined.** This repository has no published
contact address or response commitment, and inventing one here would be worse
than its absence. If you are reading this as a security policy, that part is
genuinely missing rather than omitted by oversight.

---

## 1. What blocks a build, and what does not

| Audit | Tool | Behaviour |
|---|---|---|
| Node, production tree | `npm audit --omit=dev` | **Blocks** on high or critical. No exception mechanism applies. |
| Node, full tree | `npm audit` | **Blocks** on high or critical, except advisories listed in `ACCEPTED_ADVISORIES`. |
| Python | `pip-audit` | **Reports only** (`continue-on-error: true`). |

The two Node legs exist because "is it high severity?" and "does it reach
production?" are different questions. A single `--audit-level=high` gate
conflated them: it blocked on severity while its stated rationale appealed to
build-time-versus-production, so a high-severity advisory in a build-time
dependency fell between the two and produced a red gate with no way to resolve
it other than weakening the threshold for everything.

Splitting the legs means the production gate is now **stricter** than before —
nothing shipped can be excepted, at any severity above moderate — while the
build-time tree can carry a named, justified exception without lowering the bar
for anything else.

### The staleness rule

If an advisory listed in `ACCEPTED_ADVISORIES` stops being reported, the audit
**fails**. An exception that has outlived its cause is a defect, not a relief:
it silently widens the gate for whatever advisory next happens to share that
identifier's place in the report. The failure names the entry to delete.

This is why the revisit rule below is enforced by the workflow rather than left
to memory.

---

## 2. Accepted advisories

### GHSA-vfj7-8cjw-p6xm — `braces` stack exhaustion

| | |
|---|---|
| Advisory | [GHSA-vfj7-8cjw-p6xm](https://github.com/advisories/GHSA-vfj7-8cjw-p6xm) |
| npm advisory id | 1240992 |
| Severity | high, CVSS 3.1 score 7.5 |
| Vector | `CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:H` |
| Weakness | CWE-674, uncontrolled recursion |
| Affected | `braces <= 3.0.3` |
| First patched version | **none** |
| Accepted | 2026-10-04 |

#### Why the exposure is bounded

Three independent reasons, each verifiable from the repository:

**The vulnerability class cannot affect confidentiality or integrity.** The CVSS
vector carries `C:N/I:N` and `A:H` — availability only. It is a denial of
service through deeply nested glob patterns causing unbounded recursion. There
is no code execution and no data exposure. A successful exploit crashes the
process that is doing the matching.

**The only process doing the matching is the build.** `braces` is reached
through Tailwind's file-watching and glob expansion, which runs when stylesheets
are compiled — on a developer machine or a CI runner, never in a served
request. The `AV:N` network vector in the CVSS score is irrelevant to this
usage: the patterns fed to `braces` come from this repository's own Tailwind
`content` configuration, not from any external party at any point.

**It is absent from the production dependency tree.** `npm audit --omit=dev`
reports zero vulnerabilities at every severity. Confirm with:

```bash
cd frontend
npm ls braces --omit=dev        # -> (empty)
npm audit --omit=dev            # -> found 0 vulnerabilities
```

#### The dependency chain

`braces` is not a direct dependency. It arrives only beneath `tailwindcss`,
which is declared in `devDependencies` as `^3.4.17` and resolves to `3.4.19`:

```
tailwindcss@3.4.19            (devDependency, declared ^3.4.17)
├── chokidar@3.6.0
│   └── braces@3.0.3
├── fast-glob@3.3.3
│   └── micromatch@4.0.8     (deduped)
└── micromatch@4.0.8
    └── braces@3.0.3         (deduped)
```

Reproduce with `npm ls tailwindcss chokidar micromatch fast-glob braces`.

`npm audit` reports **five** high-severity packages — `braces`, `chokidar`,
`micromatch`, `fast-glob` and `tailwindcss` — but the report contains **one**
root advisory. Only `braces` carries it; the rest are recorded as consequences,
each naming the package it inherits from rather than the advisory:

| package | `via` |
|---|---|
| `braces` | the advisory itself |
| `chokidar` | `braces` |
| `micromatch` | `braces` |
| `fast-glob` | `micromatch` |
| `tailwindcss` | `chokidar`, `fast-glob`, `micromatch` |

Collapsing those chains onto the one advisory that has to be judged is why a
single entry in `ACCEPTED_ADVISORIES` accounts for the whole failure, and why
the gate reports "5 blocking-severity package(s), 1 distinct advisory(ies)".

#### Production image impact: none

`frontend/Dockerfile.prod` is a multi-stage build. The `builder` stage installs
dependencies and compiles the application; the `runtime` stage starts from
`nginxinc/nginx-unprivileged:1.27-alpine` and copies only the compiled output:

```dockerfile
FROM node:20-alpine AS builder
WORKDIR /app
# ...
RUN npm ci --no-audit --no-fund
# ...
RUN npm run build

FROM nginxinc/nginx-unprivileged:1.27-alpine AS runtime
# ...
COPY --from=builder /app/dist /usr/share/nginx/html
COPY nginx.prod.conf /etc/nginx/conf.d/default.conf
```

(Abridged: the `# ...` markers stand for lines that do not bear on which files
reach the runtime stage.)

`node_modules` is never copied forward, so no version of `braces` exists in the
published image. The runtime image contains static assets and an nginx
configuration.

#### Why there is no small fix

Every published version of `braces` is affected. The advisory covers
`<= 3.0.3`, has no `first_patched_version`, and `3.0.3` is the newest of the 37
published versions — it is also what `dist-tags.latest` points at. There is no
version to bump to, so the lockfile-only remediation that cleared the earlier
`brace-expansion` advisory (a **different** package, pinned at `5.0.12`) has no
equivalent here.

Running the non-breaking remediation changes nothing:

```bash
npm audit fix --dry-run    # 5 high severity vulnerabilities remain
```

#### Why Tailwind 3 to 4 is deferred

The only remediation npm offers is `npm audit fix --force`, which installs
`tailwindcss@4.3.3` and is reported as `isSemVerMajor: true`. Tailwind 4 is a
rewrite: a new configuration model, a new engine, and changed class semantics.
Taking it as a security hotfix would mean a large, unreviewed change to every
stylesheet in order to resolve a build-time denial-of-service that cannot be
triggered by anything but this repository's own configuration.

The migration is worth doing on its own terms, with its own verification. It is
tracked separately and deliberately not bundled with this exception.

#### When this exception must be removed

Remove the entry from `ACCEPTED_ADVISORIES` and delete this section when **any**
of the following becomes true:

1. `braces` publishes a patched version, or the advisory gains a
   `first_patched_version`. The audit will fail as a stale exception once the
   advisory stops being reported, which is the intended prompt.
2. The Tailwind 3 to 4 migration lands, removing the chain entirely.
3. `braces` appears in the production dependency tree. The production leg of the
   audit blocks this independently and cannot be excepted, so this case fails
   the build on its own.
4. The advisory is re-scored to include a confidentiality or integrity impact,
   or a new advisory covers the same package with a different class of weakness.
   The acceptance above rests on `C:N/I:N`; if that changes, the reasoning does
   not survive it.

---

## 3. Python dependency audit

`pip-audit` runs against `backend/requirements.txt` on every push and **does not
block**. The report is published to the job summary and uploaded as an artifact
on every run.

This is a broader exemption than the Node gate's: it accepts whatever advisories
the pinned set currently carries, without naming them individually and without a
staleness check. The rationale recorded in the workflow is that upgrading the
pinned Python dependencies is a deliberate, separately verified change rather
than something a CI failure should force mid-feature.

The asymmetry is deliberate but not necessarily permanent. Bringing the Python
audit to the same shape as the Node one — a blocking gate with named, justified
exceptions — would be an improvement, and is a separate decision from the
`braces` acceptance recorded here.

---

## 4. Adding an exception

1. Establish that the advisory does not reach production:
   `npm audit --omit=dev` must not report it. If it does, stop — the production
   leg blocks unconditionally and no exception is available.
2. Establish that no non-breaking fix exists: check
   `first_patched_version` on the advisory and run `npm audit fix --dry-run`.
3. Add the advisory identifier to `ACCEPTED_ADVISORIES` in
   `.github/workflows/security.yml`.
4. Add a section to **section 2** of this document covering what the advisory
   is, why the exposure is bounded, the dependency chain, the production-image
   impact, why the real fix is deferred, and the conditions that end the
   exception.

An exception without a section here is not acceptable, and an exception whose
advisory is no longer reported fails the build.
