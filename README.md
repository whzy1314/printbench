# PrintBench

PrintBench is a local library for 3D projects: immutable revisions, recorded check evidence, rotatable STL previews, and hash-verified downloads. It does not generate models, run slicers, or control printers. This checkout includes source, tests, a synthetic example, and an optional Hermes skill, not a populated library or a Hermes plugin.

## Run locally

Requires Python 3.11+ on Linux. No package install or frontend build. Run from the checkout root:

```sh
library="$HOME/3d-projects/printbench-data"
python3 -m printbench --data "$library" serve --port 8765
```

`--data` is a global option and goes before the command. It names the library directory, not this checkout or an artifact source. If omitted, it defaults to the path shown above. Prefer an explicit path, especially when writing. Use an owned directory with no symlink components. Listing a missing library returns an empty list; a successful first publish creates it. Serving an empty or missing library does not seed it.

Open the printed URL on the machine running PrintBench. HTTP binds to `127.0.0.1` by default and is read-only, but is **not authenticated**: other local processes can read the artifacts and manifests, including recorded source paths. If the port is busy, choose another port or use `--port 0` and read the assigned URL; do not stop an unrelated listener. Press Ctrl+C to stop the server you started.

## Container

The dependency-free image runs as UID/GID 10001 and serves read-only HTTP on container loopback by default. Supply a writable volume at `/data` for that identity; the startup command creates `/data/library` with mode `0700` on first boot. Keep writers on the same filesystem and UID; `--data /data/library` is the container library. The browser listener never accepts publication. The image does not contain any library or demo artifacts.

For a trusted private proxy only, opt in with `serve --host bench.example` or set `PRINTBENCH_HOST=bench.example` (including in the container). This binds IPv4 `0.0.0.0`. Forward the exact configured lowercase DNS `Host` without a port; forwarded host headers are ignored. Loopback health probes still work. Host checking is not authentication: anyone who can reach the listener and send that Host can read artifacts and manifests. Do not expose it to untrusted clients.

### Publish to GHCR

The `Publish PrintBench image` workflow runs only on manual dispatch from `whzy1314/printbench` on `main`. It publishes the dispatched commit's `linux/amd64` image to `ghcr.io/whzy1314/printbench`, tagged with the full commit SHA, using the Actions token. It reports the immutable tag-plus-digest reference. GHCR packages start private even for public source; the package owner sets visibility to Public separately. Verify an anonymous pull of the exact reference before using it. The workflow does not change visibility or deploy anything.

### Helm chart

`charts/printbench` deploys one loopback-only replica with a `ReadWriteOnce` claim by default; it creates no Service or Ingress. Set `ingressHost` to opt in to a ClusterIP Service and one host-specific Traefik Ingress. The chart does not provide TLS or network access restrictions; arrange those before exposing the route. Supply `image` (reviewed `linux/amd64` image with `@sha256:…`, built with host opt-in support when using ingress). For a public image, omit `imagePullSecret` or set it to `""`; for a private image, supply the name of an existing pull Secret in the release namespace. The namespace is assumed to exist unless `createNamespace` is set for a dedicated namespace. The claim uses the default StorageClass unless `storageClass.name` selects another one. To create a dedicated retained StorageClass, also supply `storageClass.provisioner` and any driver-specific `storageClass.parameters` as string values. Verify the provisioner, mount and data retention before use; the chart cannot check that the claim is bound or that credentials work.

Offline chart tests require Helm and PyYAML: `python3 -m unittest discover -s tests/deployment -v`.

## Optional private management API

Management is disabled unless explicitly configured. It shares the existing store and locks, not the browser listener. A local isolated test can start it with `serve --port 0 --management-port 0 --management-token-file FILE --management-cert-file CERT --management-key-file KEY`; use an owner-only synthetic token and a disposable certificate valid for `localhost`. The CLI prints the loopback management HTTPS origin. No plaintext management or certificate-check override is offered by the CLI. For an actual network binding, also configure `--management-host` and `--management-proxy-ca-file`; network management requires native TLS and a peer certificate from a dedicated proxy-client CA *before* examining the bearer or Host. The server requires the exact configured Host and ignores forwarded headers.

Chart opt-in is `management: {host: ..., tokenSecret: ..., tlsSecret: ...}`. This adds a management-only ClusterIP Service at 443 and read-only Secret mounts to the same Deployment, not an Ingress or an additional claim. `tokenSecret` must have key `token` (a dedicated, random 32-byte token encoded as 64 lowercase hex characters, with at most one trailing LF). `tlsSecret` must have `tls.crt`, `tls.key`, and `ca.crt` (the dedicated proxy-client CA). Verify UID/GID and effective protected mount permissions: root:10001 `0440` on the projected files; local client/server token files must be regular owner-only `0400` or `0600` for their respective users. The app rereads the token for each request and before committing an upload; rotation of TLS key/cert/CA needs a restart. Never place credential contents in values, env vars, CLI arguments, logs or shell output. A missing or unsafe token denies management.

Use only an authorized private HTTPS edge that verifies the backend cert/SNI, holds the dedicated proxy client key and restricts management peers, methods and paths. Do not trust headers as transport or identity proof. Do not route management over the read Service or a plaintext backend hop. At the edge, prohibit cache, redirects and automatic mutation retries, cap upload bodies at 68,157,444 bytes, bound header/body time and concurrency, and never log auth headers or bodies. Verify unauthorized/direct paths, token revocation and unchanged read ingress before activation. See [the bundled skill](skills/printbench/SKILL.md) for client commands. Use an approved origin with `python3 -m printbench.client --url ...`; an optional `--ca-file` must point at the operator-approved trust bundle. The client never follows redirects or attaches the token to a read API request.

| Authenticated request | Effect |
| --- | --- |
| `GET /manage/v1/projects/ID/state` | Current revision ID, archive flag and strong state `ETag`; no manifest or artifacts |
| `PUT /manage/v1/projects/ID` with `If-None-Match: *` | Create with first immutable revision only |
| Same `PUT` with the last observed `If-Match: "…"` | Append a new immutable revision only |
| `POST /manage/v1/projects/ID/archive` or `/restore` with `If-Match` | Atomically change visibility without moving the current revision or rewriting history |

The `PUT` frame is `Content-Type: application/octet-stream` with one exact decimal `Content-Length`: 4-byte big-endian JSON byte count, then UTF-8 JSON `{"spec": {...}, "sizes": [...]}`, then full artifact bytes in `spec.artifacts` order. JSON is limited to 1 MiB and combined artifacts to 64 MiB; malformed framing, unsafe filenames, encodings, duplicate JSON keys, chunked bodies and `Expect` are rejected. The local CLI publication limit is unchanged. State-only requests have no body. A stale precondition returns `412`, missing precondition `428`, duplicate revision or conflicting state `409`. No write is automatically retried. A lost response is ambiguous: re-read state and verify the exact revision, statuses and hashes; never refresh a precondition and replay a stale operation automatically. An archived project is absent from ordinary list, detail, history, revision and download/HEAD paths; the authenticated state endpoint exists for restore. Archiving is not recall of previously downloaded files or a read already in flight. No purge is provided.

Rollback: close only management at the edge, drain in-flight requests and revoke the bearer, then omit chart `management` and verify the listener is gone. Keep the same claim, history, read route and an archive-aware reader. A pre-management binary ignores archive state and is not a safe rollback once archives exist.

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
