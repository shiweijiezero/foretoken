<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Release Versions

Foretoken uses Python packaging version notation for the Python distribution and the same release identifier for GitHub tags and OCI image tags.

| Stage | Version | GitHub tag | Install from PyPI |
| --- | --- | --- | --- |
| Alpha | `0.0.1a1` | `v0.0.1a1` | `pip install --pre foretoken` |
| Beta | `0.0.1b1` | `v0.0.1b1` | `pip install --pre foretoken` |
| Release candidate | `0.0.1rc1` | `v0.0.1rc1` | `pip install --pre foretoken` |
| Stable | `0.0.1` | `v0.0.1` | `pip install foretoken` |

`pip` does not select pre-release versions by default. Use `--pre` for Alpha, Beta, and Release Candidate builds, or request an exact version such as `foretoken==0.0.1a1`.

Source installation is separate from package releases:

```bash
pip install -e .
```

## OCI image tags

Use the same release identifier for the Foretoken images and Helm Chart:

```text
0.0.1a1
0.0.1b1
0.0.1rc1
0.0.1
```

`latest` is a mutable development alias. Use an exact version tag for stable deployments, rollback, and reproducible installations.

## Release sequence

1. Update `pyproject.toml` to the next PEP 440 version.
2. Build and verify the Python distribution and affected OCI images.
3. Push the matching image and Chart tags.
4. Create the matching GitHub tag and Release.
5. Let the release workflow publish the Python distribution to PyPI.
6. Verify the published package, images, Chart, and a clean installation path.
