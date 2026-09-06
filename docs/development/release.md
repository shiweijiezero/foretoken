<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Release Versioning

Foretoken publishes a Python distribution, OCI images, and a Helm Chart. Python packages follow PEP 440, while OCI images and Helm Charts use SemVer. A release keeps the same stage and sequence number across both formats even though their spelling differs.

## Version stages

| Stage | Purpose | Python version | OCI and Helm version |
| --- | --- | --- | --- |
| Development | Identifiable local or CI snapshot | `0.0.1.dev1` | `0.0.1-dev.1` |
| Alpha | Early integration and interface testing | `0.0.1a1` | `0.0.1-alpha.1` |
| Beta | Feature-complete compatibility and deployment testing | `0.0.1b1` | `0.0.1-beta.1` |
| Release candidate | Final validation before a stable release | `0.0.1rc1` | `0.0.1-rc.1` |
| Stable | Supported release for normal installation | `0.0.1` | `0.0.1` |
| Python post-release | Correction to an already published Python artifact or its metadata | `0.0.1.post1` | Normally reuse `0.0.1`; use the next patch if platform artifacts change |

Increment the final number when publishing another build in the same stage: `0.0.1a2`, `0.0.1b2`, or `0.0.1rc2`. Move to the next stage only when the release meets that stage's purpose.

Python orders these versions as follows:

```text
0.0.1.dev1 < 0.0.1a1 < 0.0.1b1 < 0.0.1rc1 < 0.0.1 < 0.0.1.post1
```

Development snapshots are not GitHub Releases and are not published to PyPI. OCI images may additionally maintain `latest` as a mutable alias for normal source iteration; `latest` is not a Helm Chart version or a release version.

## Installing Python releases

Alpha, Beta, and Release Candidate versions are pre-releases. `pip` excludes them from normal version selection unless a pre-release is requested:

```bash
pip install --pre foretoken
pip install foretoken==0.0.1a1
```

Stable and post-release versions use the normal installation command:

```bash
pip install foretoken
```

A `.postN` release normally reuses the matching Stable platform artifacts because it only corrects the published Python package or its metadata. Do not use it for normal code changes. If runtime behavior or platform artifacts must change, publish the next patch version, such as `0.0.2`, instead of placing those changes in `.postN`.

Installing from the repository is independent of published versions:

```bash
pip install -e .
```

## Tags and version ownership

GitHub Releases use the Python version with a `v` prefix because the release workflow publishes that Python distribution:

```text
v0.0.1a1
v0.0.1b1
v0.0.1rc1
v0.0.1
v0.0.1.post1
```

Each artifact has one authoritative version source:

- `pyproject.toml` owns the Python distribution version.
- `deploy/charts/foretoken/Chart.yaml` owns the Helm `version` and `appVersion`.
- Foretoken OCI images and the Helm Chart package use the SemVer value for the same release stage.

Published versions are immutable. Never rebuild and overwrite a version already present on PyPI or in an OCI registry. Increment the development, pre-release, post-release, or patch number as appropriate.

## Release sequence

1. Choose the release stage and update `pyproject.toml` and `Chart.yaml` using the mapping above.
2. Build and verify the Python distribution, Helm Chart, and affected OCI images.
3. Push the matching OCI image and Helm Chart tags.
4. Create the matching GitHub tag and publish the GitHub Release.
5. Let the release workflow publish the Python distribution to PyPI.
6. Verify the published package, images, Chart, and a clean installation path.
