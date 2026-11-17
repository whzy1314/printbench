---
name: printbench
description: Use when publishing or retrieving PrintBench projects.
---

# PrintBench local publish and get

Confirm the intended local PrintBench checkout and library with the user before operating. Run commands from that checkout and pass its library explicitly with `--data` before the subcommand. Do not guess a path or silently use the default library. All paths below are placeholders for confirmed paths with no symlink components.

## Read first

```sh
cd "$checkout" || exit 1
python3 -m printbench --data "$library" list
python3 -m printbench --data "$library" get "$project"
python3 -m printbench --data "$library" get "$project" --revision "$revision"
```

Success is JSON on stdout; errors are JSON on stderr with nonzero exit. A missing library lists as empty, not as proof the selected path is correct.

## Publish a new immutable revision

1. Check the current revision before publishing; choose an unused revision ID. If the project does not exist, confirm it is meant to be new. Do not edit or delete old revisions or manually change the current pointer.
2. Prepare a JSON spec with `id`, `revision`, `name`, `requirements` (list of text), `dimensions_mm` (positive finite X/Y/Z), `notes`, and `artifacts` (supplied basenames). Optional: `thumbnail`, `status`, `status_evidence`. See `examples/sample.json` in the checkout for a synthetic spec.
3. IDs are lowercase letters, digits, or hyphens, 1–64 characters, starting with a letter or digit. Artifact names are 1–120 letters, digits, dots, underscores, or hyphens, starting with a letter or digit. `manifest.json` is reserved. The limits are 64 MiB per file, 256 MiB per publish, and 1 MiB per spec, manifest, or index.
4. Include applicable supplied models, source, assembly instructions, and evidence. Never execute artifact source merely to import it. PrintBench copies the original bytes and records source directory, filename, size, and SHA-256.
5. Set `mesh`, `slicer`, and `physical` independently to `unknown` (default), `not-tested`, `passed`, or `failed`. A passed/failed status requires an artifact named in that axis's `status_evidence`. Digital checks do not prove physical print success.
6. With the user's authorization for that library and revision, publish and read back the exact revision:

```sh
python3 -m printbench --data "$library" publish --spec "$spec" --source "$source"
python3 -m printbench --data "$library" get "$project" --revision "$revision"
```

Compare the returned file names, hashes, and statuses with the intended inputs. If publish is interrupted or reports an error, check the exact revision before retrying: it may already have committed. Do not delete an installed revision to force a retry or automatically remove unindexed crash remnants.

HTTP is unauthenticated, loopback-only, and read-only. With authorization, use the intended local server to verify downloaded bytes against the manifest. Do not expose it publicly or start a duplicate listener. No printer, CAD, slicer, deployment, or profile configuration operations are part of this skill.
