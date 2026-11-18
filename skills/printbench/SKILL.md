---
name: printbench
description: Use when the 3D Designer needs to retrieve, publish, archive, or restore a hosted PrintBench project or revision.
---

# Hosted PrintBench library

The operator-confirmed hosted HTTPS read origin is the library of record. From the reviewed checkout run `python3 -m printbench.client`. Do not guess an endpoint, publish into a local fallback, or use cluster credentials. Use the approved HTTPS management origin (which may be the same as read) only after the operator confirms the route and grants the operation. Missing route, TLS trust, or credential means stop; do not work around it. Do not change profiles, infrastructure, printers, slicers, or CAD from this workflow.

## Read and verify

```sh
cd "$checkout" || exit 1
python3 -m printbench.client --url "$read_origin" list
python3 -m printbench.client --url "$read_origin" history "$project"
python3 -m printbench.client --url "$read_origin" get "$project" --revision "$revision"
python3 -m printbench.client --url "$read_origin" download "$project" --revision "$revision" "$artifact" --output "$new_file"
```

Downloads verify length and SHA-256 against the exact revision manifest and will not overwrite a file. Compare against intended input hashes and report project, revision, status, and evidence. An approved custom CA uses `--ca-file` before the subcommand; never disable TLS verification.

## Write only with authorization

Use `--token-file "$protected_file"` before the subcommand. The operator delivers an owner-only `0400` or `0600` regular file; pass only its path. Never read the value into chat, env, argv, scripts, logs or Git. Browser reads do not need the token.

1. Read current/history and select a fresh revision ID. Confirm creation if absent. Build the spec as in `examples/sample.json` from explicit, safe basenames (no paths, symlinks or reserved `manifest.json`). Use only supplied artifacts: max 64 MiB combined, 1 MiB JSON metadata. Do not execute source merely to import it.
2. Record `mesh`, `slicer` and `physical` separately as `unknown`, `not-tested`, `passed` or `failed`. Each `passed`/`failed` needs a relevant supplied evidence artifact. Digital evidence does not prove physical success.
3. Create only with `create --spec "$spec" --source "$source"`. For an existing project, use `state "$project"` to read its exact quoted `etag`, then `append --spec "$spec" --source "$source" --match "$etag"`. Both are subcommands of `python3 -m printbench.client --url "$management_origin" --token-file "$protected_file"`. Never edit or replace an old revision.
4. Read the exact revision from the ordinary read origin to verify it. On a timeout, conflict, or interrupted response, first read authenticated state and verify the exact revision and hashes through the read origin. If already committed, report that; if archived or uncertain, stop and report the ambiguity. Never automatically fetch a new tag and replay the write.

## Reversible visibility

With authorization, use `state` then `archive "$project" --match "$etag"` or `restore "$project" --match "$etag"` from the management origin. Re-read state and verify ordinary list, direct revision and download visibility. Archive hides every revision without changing current/history; restore reverses that. No purge. To correct published content, publish another immutable revision. Service rollback is operator-owned: disable management while retaining archive-aware readers and the same data.
