# PrintBench

PrintBench is a local library for 3D projects: immutable revisions, recorded check evidence, rotatable STL previews, and hash-verified downloads. It does not generate models, run slicers, or control printers. This checkout includes source, tests, a synthetic example, and an optional Hermes skill, not a populated library or a Hermes plugin.

## Run locally

Requires Python 3.11+ on Linux. No package install or frontend build. Run from the checkout root:

```sh
library="$HOME/3d-projects/printbench-data"
python3 -m printbench --data "$library" serve --port 8765
```

`--data` is a global option and goes before the command. It names the library directory, not this checkout or an artifact source. If omitted, it defaults to the path shown above. Prefer an explicit path, especially when writing. Use an owned directory with no symlink components. Listing a missing library returns an empty list; a successful first publish creates it. Serving an empty or missing library does not seed it.

Open the printed URL on the machine running PrintBench. HTTP binds to `127.0.0.1` and management is disabled by default. Browser reads are **not authenticated**: other local processes can read the artifacts and manifests, including recorded source paths. If the port is busy, choose another port or use `--port 0` and read the assigned URL; do not stop an unrelated listener. Press Ctrl+C to stop the server you started.

## Container

The dependency-free image runs as UID/GID 10001 and serves read-only HTTP on container loopback by default. Supply a writable volume at `/data` for that identity; the startup command creates `/data/library` with mode `0700` on first boot. Keep writers on the same filesystem and UID; `--data /data/library` is the container library. Browser and `/api/` routes remain GET/HEAD-only even if management is enabled. The image does not contain any library or demo artifacts.

For a trusted private proxy only, opt in with `serve --host bench.example` or set `PRINTBENCH_HOST=bench.example` (including in the container). This binds IPv4 `0.0.0.0`. Forward the exact configured lowercase DNS `Host` without a port; forwarded host headers are ignored. Loopback health probes still work. Host checking is not authentication: anyone who can reach the listener and send that Host can read artifacts and manifests. Do not expose it to untrusted clients.

### Publish to GHCR

The `Publish PrintBench image` workflow runs only on manual dispatch from `whzy1314/printbench` on `main`. It publishes the dispatched commit's `linux/amd64` image to `ghcr.io/whzy1314/printbench`, tagged with the full commit SHA, using the Actions token. It reports the immutable tag-plus-digest reference. GHCR packages start private even for public source; the package owner sets visibility to Public separately. Verify an anonymous pull of the exact reference before using it. The workflow does not change visibility or deploy anything.

### Helm chart

`charts/printbench` deploys one loopback-only replica with a `ReadWriteOnce` claim by default; it creates no Service or Ingress. Set `ingressHost` to opt in to a ClusterIP Service and one host-specific Traefik Ingress. The chart does not provide TLS or network access restrictions; arrange those before exposing the route. Supply `image` (reviewed `linux/amd64` image with `@sha256:…`, built with host opt-in support when using ingress). For a public image, omit `imagePullSecret` or set it to `""`; for a private image, supply the name of an existing pull Secret in the release namespace. The namespace is assumed to exist unless `createNamespace` is set for a dedicated namespace. The claim uses the default StorageClass unless `storageClass.name` selects another one. To create a dedicated retained StorageClass, also supply `storageClass.provisioner` and any driver-specific `storageClass.parameters` as string values. Verify the provisioner, mount and data retention before use; the chart cannot check that the claim is bound or that credentials work.

Offline chart tests require Helm and PyYAML: `python3 -m unittest discover -s tests/deployment -v`.

For Helm CLI upgrades, use `helm upgrade RELEASE ./charts/printbench --reset-values -f desired-values.yaml` with the **complete desired values**, including the image and every override to retain. Do not use `--reuse-values` or `--reset-then-reuse-values` for these security settings. In Helm 3.18.3, `--reuse-values` can discard an explicit `null` and retain the previous `management.shutdownProbe` or `management.tokenSecret` before the chart can validate it. Argo CD renders fresh from the full declarative values, not from Helm release history. For either supported path, omit `management` to disable management entirely, or keep `tokenSecret` and omit `shutdownProbe` (or set it to `false`) to disable only the probe. `null` is not a disable value.

## Optional private management API

Management is disabled unless explicitly configured. It uses `/manage/v1/` on the existing HTTP listener and shares the existing store and locks. Opt in with `serve --management-token-file FILE` or `PRINTBENCH_MANAGEMENT_TOKEN_FILE` (path only). Use synthetic credentials for isolated loopback tests. The server checks the actual Host and authenticates every management operation with the bearer before processing a body, including on direct requests. Forwarded headers never establish identity, authorization, or transport security. The old separate-listener settings must not be reused.

Chart opt-in is `management: {tokenSecret: ...}`. It adds only a read-only Secret projection and path variable to the same Deployment, without changing listeners, routing or storage. `tokenSecret` must have key `token` (a dedicated, random 32-byte token encoded as 64 lowercase hex characters, with at most one trailing LF). Verify effective projected permissions: root:10001 `0440`; local client/server token files must be regular owner-only `0400` or `0600`. Missing, malformed, or unsafe credentials prevent enabled startup. The app rereads the token for each management request and before committing an upload. Never place credential contents in values, env vars, CLI arguments, logs, shell output or the browser.

Use the hosted CLI only with an approved, certificate-verified private HTTPS edge. **TLS at the edge does not protect the bearer on a plaintext backend hop: an observer there can capture and reuse it, including on the direct HTTP app path.** Independently verify the private transport/logging assumptions and revocation before activation. The edge must not log credential headers or bodies, cache management responses, or retry mutations. See [the bundled skill](skills/printbench/SKILL.md) for client commands. `python3 -m printbench.client --url ...` verifies HTTPS, never follows redirects, and attaches the bearer only to management calls. The browser remains credential-free.

| Authenticated request | Effect |
| --- | --- |
| `GET /manage/v1/projects/ID/state` | Current revision ID, archive flag and strong state `ETag`; no manifest or artifacts |
| `PUT /manage/v1/projects/ID` with `If-None-Match: *` | Create with first immutable revision only |
| Same `PUT` with the last observed `If-Match: "…"` | Append a new immutable revision only |
| `POST /manage/v1/projects/ID/archive` or `/restore` with `If-Match` | Atomically change visibility without moving the current revision or rewriting history |

The `PUT` frame is `Content-Type: application/octet-stream` with one exact decimal `Content-Length`: 4-byte big-endian JSON byte count, then UTF-8 JSON `{"spec": {...}, "sizes": [...]}`, then full artifact bytes in `spec.artifacts` order. JSON is limited to 1 MiB and combined artifacts to 64 MiB; malformed framing, unsafe filenames, encodings, duplicate JSON keys, chunked bodies and `Expect` are rejected. The local CLI publication limit is unchanged. State-only requests have no body. A stale precondition returns `412`, missing precondition `428`, duplicate revision or conflicting state `409`. No write is automatically retried. A lost response is ambiguous: re-read state and verify the exact revision, statuses and hashes; never refresh a precondition and replay a stale operation automatically. An archived project is absent from ordinary list, detail, history, revision and download/HEAD paths; the authenticated state endpoint exists for restore. Archiving is not recall of previously downloaded files or a read already in flight. No purge is provided.

Reads do not wait for the writer lock: each project is read from one committed index snapshot and its immutable revision files. An overlapping read can finish from its earlier snapshot; requests started after archive commits cannot read the project. A list takes one snapshot per project, not a library-wide transaction. State ETags bind the complete committed index and project ID, including legacy indexes (not rewritten on read).

Rollback: first close admission to all management writes (including direct backend paths; revoking protected token delivery can close new bearer admission) and pause local CLI writers. Drain admitted requests or prove their settlement, including reconciling ambiguous outcomes against the committed state; a quiet edge or a temporarily free writer lock alone is not settlement. Keep writes closed if settlement is unproven. Omit chart `management` and verify management dispatch is disabled on the existing listener. Keep the same claim, history, read route and an archive-aware reader; verify archived IDs still fail direct reads/downloads and are absent from the list. Never restore archived projects or install an archive-unaware binary as part of rollback.

### Same-process writer shutdown evidence

Opt in on the writer at startup with `PRINTBENCH_MANAGEMENT_SHUTDOWN_PROBE=1` (in Helm, set `management.shutdownProbe: true` alongside `management.tokenSecret`; incompatible with sentinel proof mode). Before rotation, use the exact app Host and a bodyless `GET /manage/v1/projects/ID/state` for a known project, with exactly one `Authorization` header carrying the current bearer A and `X-PrintBench-Shutdown-Arm: 1`. Require the normal `200` state response. The first qualified, accepted arm holds one process-private equality verifier for ten minutes; it cannot be refreshed or rebound. Restarting loses the arm.

Pause writers and replace the existing projected token with a fresh valid B, withheld from **every** writer. Keep B projected and the same writer running. Repeat the bodyless state GET with A and the same Host and pathname over each required path, omitting the arm header. Require the ordinary `401`, `WWW-Authenticate: Bearer realm="PrintBench"`, JSON `Credential required`, `Cache-Control: no-store`, and exactly one fresh `X-PrintBench-Shutdown-Receipt` (32 lowercase hex characters). If A is still current the GET remains a normal `200`. Do not send B or use an untrusted hop for A.

The receipt means this writer saw A, denied it against the replacement, and held both app management permits at the fresh credential check. A bare denial is not evidence: a proxy could strip A, the projection could be unavailable, or an earlier handler could still be uploading or waiting to commit. Require positive settlement of upstream and local CLI work and reconcile every uncertain outcome without replay. If any evidence is missing or ambiguous, keep admissions closed and do not replace the writer.

### Non-writing LAN auth proof

For a separately authorized proof on the existing LAN HTTPS route, use **only** `management: {proofSecret: printbench-p5-lan-proof}` (omit `tokenSecret`). The chart references an existing namespace-local Secret with key `sentinel`; it adds only a read-only `0440` projection and `PRINTBENCH_MANAGEMENT_PROOF_FILE=/run/printbench/token/sentinel` to the Deployment. The file must contain a freshly generated **synthetic** 32-byte sentinel in the same lowercase hex format as above, never an issued writer credential. For local loopback, set that environment variable to the protected file path. A missing, malformed or unsafe file prevents startup; proof and writer settings together are an error.

A bodyless `GET /manage/v1/projects/ID/state` with the exact configured Host and exactly one `Authorization: Bearer …` returns `401`. An exact match to the currently nominated sentinel adds `X-PrintBench-Proof-Receipt` (fresh random 32 lowercase hex characters). No state is returned, and no request data is echoed in the receipt. Require `Cache-Control: no-store`, the PrintBench challenge and exactly one fresh receipt on the response to that specific request; use one request per connection without redirects or retries. Missing, wrong, ambiguous and malformed requests never get receipts. All management mutations are rejected without consuming their bodies, even with the sentinel. Ordinary reads are unchanged; the local publish CLI is not disabled.

For the reload control, rotate from startup sentinel A to a different synthetic sentinel B, verify the intended projection generation, then send only A. For five minutes from startup, an exact match to A against a valid, different current nomination returns the same denial with `X-PrintBench-Retired-Proof-Receipt` instead. The app retains its startup copy of A only in process memory. Require exactly one new receipt and a fresh matching JSON line (`receipt` and fixed `verdict: retired` only) from that same process's stderr for this exchange. A cached/replayed response, missing event, or event from an earlier request is not proof. A process started with B cannot receipt retired A.

This proves what the app observed and denied **in proof mode only**. A bare `401`, `503`, missing receipt, or proxy failure does not establish what reached the app. Verify native forwarding, no header logging/caching, and direct/off-LAN controls separately before relying on the LAN result. This mode does not prove revocation or in-flight settlement on a running writer; writer rollback needs separate app-attributed old-credential denial before replacing that writer, plus the settlement checks above. Do not switch to proof mode as a substitute for that gate.

## Try a synthetic project

Run from the checkout root. This uses a new disposable library, not your default library:

```sh
demo=$(mktemp -d) || exit 1
mkdir "$demo/source"
printf '%s\n' 'Synthetic PrintBench download example.' > "$demo/source/sample.txt"
python3 -m printbench --data "$demo/library" publish \
  --spec examples/sample.json --source "$demo/source"
python3 -m printbench --data "$demo/library" get sample --revision r1
python3 -m printbench --data "$demo/library" list
python3 -m printbench --data "$demo/library" serve --port 0
```

The example has no model or test pass claims. After stopping that server, you can remove the disposable directory you just created. To import your own project, adapt the JSON spec: `id`, `revision`, `name`, `requirements`, `dimensions_mm` (positive finite X/Y/Z), `notes`, and `artifacts` are required. `thumbnail` may name a supplied PNG/JPEG. `status` records `mesh`, `slicer`, and `physical` independently; each defaults to `unknown`. `passed` and `failed` require a matching `status_evidence` entry naming a supplied artifact. This is recorded evidence, not validation by PrintBench.

Artifacts must be regular files with safe basenames (letters, digits, dots, underscores, or hyphens; start with a letter or digit). `manifest.json` is reserved. Limits: 64 MiB/file, 256 MiB/publish, and 1 MiB per input spec, serialized manifest, or project index. Input files are copied, never executed or moved. The browser preview reads binary or ASCII STL only (20 MiB / 50,000 triangles); it interprets coordinates as millimetres, not measured dimensions. Other supplied artifact types can be downloaded.

CLI successes are JSON on stdout; validation/filesystem failures are JSON on stderr with a nonzero exit. New content needs a new revision ID. If a write fails or is interrupted, check the exact revision with `get` before retrying; the commit may already be visible. Do not edit manifests or turn an existing revision into a different one.

## Back up and restore

The library's `project.json` indexes, `revisions/` directories, manifests, and artifacts belong together. Back up the **whole library**, separately from this source checkout. Stop or otherwise quiesce all writers before copying. Use a new owned path on trusted backup storage; for example, after setting `backup_dir` to an unused absolute directory path:

```sh
mkdir -m 700 -- "$backup_dir" && cp -a -- "$library" "$backup_dir/library"
```

To verify or restore, copy the backup to a separate new directory (never on top of an active library), then use `--data` to inspect it before switching readers or writers:

```sh
mkdir -m 700 -- "$restore_dir" && cp -a -- "$backup_dir/library" "$restore_dir/library"
python3 -m printbench --data "$restore_dir/library" list
python3 -m printbench --data "$restore_dir/library" get YOUR_PROJECT --revision YOUR_REVISION
```

Set `restore_dir` to an unused absolute path first. Compare the restored project/revision list and verify important downloads against their manifest hashes; `list` alone does not verify artifact bytes. Stop and inspect if copying or verification fails. Revisions are immutable through PrintBench, not protected from OS-level edits. Unindexed remnants after a crash are not published; do not remove them without inspecting and backing up the library.

## Optional Hermes skill

The standalone app does not need Hermes. For the [bundled skill](skills/printbench/SKILL.md), use Hermes in the **intended profile**. Check for an existing `printbench` skill (including disabled skills) with `hermes -p YOUR_PROFILE skills list`; do not overwrite it without separately reviewed authorization. Use the intended profile's native skill tools and verify readback with `skill_view(name="printbench")`; do not bypass profile controls. Supply the reviewed checkout, approved HTTPS origin and protected credential path separately. Skill installation does not enable writes, start a server or create a library.

## Tests

```sh
python3 -m unittest discover -s tests -v
node --test tests/viewer.test.mjs
node --check web/app.js
python3 -m compileall -q printbench
```

Node.js 22 is sufficient for these checks; it is not a runtime dependency. Tests use temporary files and ephemeral loopback ports, not the default library.
