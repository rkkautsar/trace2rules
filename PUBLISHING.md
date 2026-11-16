# Publishing

The package name `trace2rules` is available on PyPI as of 2026-11-16.

## One-time PyPI setup

Configure a PyPI trusted publisher for the GitHub repository:

- Owner: `rkkautsar`
- Repository: `trace2rules`
- Workflow: `publish.yml`
- Environment: `pypi`

Create the `pypi` environment in GitHub repository settings if it does not
exist.

## Release

1. Update `version` in `pyproject.toml`.
2. Commit the version bump.
3. Create and push a matching tag:

```bash
git tag v0.1.0
git push origin v0.1.0
```

The `publish` workflow verifies that the tag matches `pyproject.toml`, builds
the sdist and wheel, and publishes with PyPI trusted publishing.

## Manual publish

```bash
uv build
uv publish --token "$PYPI_TOKEN"
```
