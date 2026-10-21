# User provisioning API

The Admin API can create an OPS tenant on behalf of an external provisioning
system. The contract implements issue nuvolaris/projects#447. It is independent
of tenant AUTH keys and SSO login. Existing login, CLI and SSO APIs retain their
behavior. Installing this image alone does not enable provisioning: the external
system must create the authorization Secret.

## Enable and authorize

The external deployment manages an `Opaque` Secret named
`openserverless-user-provisioning-token` in namespace `openserverless`. Its
`data.token` field contains the base64-encoded provisioning token. Generate at
least 32 random bytes of entropy. Do not use a tenant key, user password, or
service-account token. Only the external deployment and authorized operators
should have access to the value.

For an isolated lab, an operator can create the Secret from a protected file:

```sh
umask 077
# Generate the token without a trailing newline; keep this file private.
python3 -c 'import secrets; print(secrets.token_hex(32), end="")' > provisioning-token
kubectl -n openserverless create secret generic openserverless-user-provisioning-token \
  --from-file=token=provisioning-token
kubectl apply -f deploy/user-provisioning/rbac.yaml
```

This is an explicit operator action, not application startup behavior. In a
managed installation let the external deployment own the Secret instead.
The optional Role/RoleBinding grants read access to this exact Secret, read
access to the controller configuration and base ingress, and create/read access
needed for users and their routes in `openserverless`. Check the service-account
subject against the actual Admin API pod. Existing broader roles are not changed
or narrowed by this manifest. Kubernetes RBAC cannot restrict `create` by
`resourceNames`; ingress and middleware creation is limited to this namespace.

Each API request reads the Secret again. Replacing its token revokes the old
value for subsequent authorization checks without a restart. Removing the
Secret disables new provisioning requests. Requests already authorized may
complete. An empty, malformed, missing or unreadable Secret fails closed;
it never falls back to a built-in credential. Normal startup and other APIs do
not require the Secret.

## Request

Use HTTPS outside isolated local testing. Send the token only through the
Authorization header, never in the URL. The caller should obtain the token from
its secret store and keep request bodies and headers out of application logs.

```http
POST /system/api/v1/users
Authorization: Bearer <provisioning-token>
Content-Type: application/json

{
  "user": "exampleuser",
  "password": "<initial-password>",
  "email": "user@example.org",
  "services": "all"
}
```

`user`, `email`, and `password` must be non-empty strings without control
characters. Names follow the existing Admin API validator: 5–61 lowercase ASCII
letters/digits, without rewriting, with infrastructure names reserved. Email
must be an ASCII dot-atom `local@domain.tld` address (no quoted local parts or
backslash escapes), at most 254 characters, with a local part at most 64
characters. This also avoids unsafe interpolation in the existing operator
metadata template.
When MongoDB is selected, user names are limited to 54 characters for FerretDB
role/database suffix compatibility; object storage limits them to 58 characters
so the generated bucket names fit the S3 limit. These checks occur before user
creation. Other selections retain the 61-character Admin API limit.

Passwords are limited to 72 UTF-8 bytes to avoid bcrypt truncation; they are
never trimmed. Requests are limited to 16 KiB. Unknown properties are rejected.

`services` is optional and defaults to `all`: all tenant services enabled in the
controller's `spec.components`. Missing optional components are treated as
disabled; the API never enables or installs cluster components. Explicit subsets
are comma-separated strings containing `redis`, `mongodb`, `postgres`,
`seaweedfs` and/or `milvus`. `minio` is accepted only on clusters whose controller
configuration explicitly enables that legacy backend. Both object-storage
backends cannot be enabled at the same time.

SeaweedFS/MinIO selects the tenant data and website buckets. Existing quota
defaults are used, including storage quota `auto`. Unknown, disabled, duplicate,
empty, or mixed `all,redis` selections fail before creating anything. There is
no new quota override or "none" selector in this API.

## Result and lifecycle

```json
{
  "status": "ok",
  "message": "User provisioning accepted",
  "user": "exampleuser",
  "provisioning_status": "pending",
  "services": ["redis", "mongodb", "postgres", "seaweedfs", "milvus"]
}
```

HTTP `202` confirms that a new WhiskUser was created and its additional routes
were submitted. The operator asynchronously provisions the OpenWhisk subject,
service credentials, databases, buckets, profile metadata and standard tenant
API/static routes. The API creates CLI-equivalent additional Streamer and S3
routes with WhiskUser owner references, so Kubernetes garbage-collects these
routes when the user is deleted. Streamer routes are added only if its service
exists. nginx and Traefik ingress classes are supported. S3 paths are never
rewritten, preserving SigV4 signatures.

This is not a readiness guarantee. Use the existing administrator tooling to
inspect reconciliation and the existing password login/profile flow once it
finishes. No status endpoint or credentials are added to the create response.
The input password is retained as supplied, but all service secrets and tenant
AUTH values use cryptographically secure randomness.

User creation uses POST/create, never apply or upsert. An existing, blocked, or
deleting WhiskUser returns `409`. Concurrent creates yield one winner and a
conflict; no existing account's password, services, or routes are changed.

If route submission fails after user creation, the API returns `503` with
`User created but route setup failed; administrator review required`. The user
is retained because reconciliation may already be running. Do not blindly
retry: the name now exists, so subsequent create calls return `409`. An
administrator should inspect the tenant, resolve ingress permissions/conflicts,
and reconcile the CLI routes (or explicitly remove this new tenant before
retrying). The API does not claim atomicity across multiple Kubernetes objects,
undo provisioning, overwrite existing routes, or perform a background retry.
A transport failure during user creation can also have an ambiguous outcome;
check the WhiskUser before attempting recovery.

| HTTP | Meaning |
| --- | --- |
| 202 | New WhiskUser created; provisioning pending |
| 400 | Invalid payload or service selection |
| 401 | Missing or invalid provisioning Bearer token |
| 409 | User already exists (including a concurrent creation conflict) |
| 413 | Request exceeds 16 KiB |
| 415 | Request content type is not JSON |
| 503 | Feature unconfigured, dependency unavailable, or explicit partial route failure |
| 500 | Unexpected internal error |

Errors use the existing `{"status":"ko","message":"..."}` envelope.
Authentication happens before account-existence checks. Passwords, provisioning
tokens, tenant AUTH values, and service credentials are neither logged nor
returned by the new path. See `/system/apidocs/` for the OpenAPI contract.

## Validation

Run `python -m unittest discover -s tests -v` in the project's Python environment.
The dedicated provisioning suite covers full/slim service resolution, subsets,
credential isolation, authorization, rotation/revocation, concurrent creation,
Kubernetes failure distinctions, malformed/oversized input, redacted logs,
partial route failures, and nginx/Traefik TLS/S3 route contracts. The legacy
client and login/SSO helpers are exercised by their existing suites.

Before publication, also verify the real Kubernetes RBAC and operator flow in
the lab: provisioning, readiness, password login, each enabled service, routes,
rotation, duplicate protection, and unchanged Alpha/Beta application suites.
MinIO and live nginx require separate installations; do not infer runtime
coverage for them from Traefik/SeaweedFS tests.

## Candidate publication and testing on another server

The personal fork's `feature/user-provisioning-447` branch has a dedicated
publication workflow. After the security review gate, pushing this branch builds
from its committed Dockerfile and source, runs the complete Admin API unittest
suite in the resulting image, and publishes **only**:

```text
ghcr.io/miki3421/openserverless-admin-api:candidate-user-provisioning-447-<full-commit-sha>
```

Dependencies are installed from `uv.lock` with `uv sync --frozen --no-dev` during
the build. The container starts the installed Python interpreter directly;
startup does not resolve or download dependencies. CI also verifies startup with
networking disabled and an unauthenticated request returning `401`.

The workflow is restricted to that exact branch in
`miki3421/openserverless-admin-api`. It does not update `latest`, `advanced`,
release tags, Apache images, or cluster image pins. Its Actions summary gives the
image digest; use `ghcr.io/miki3421/openserverless-admin-api@sha256:...` when
installing a specific candidate. The automated tests do not replace the Daybreak
review. This candidate is based on the personal `advanced` source, not an
Apache 0.9.0-only backport. Publication currently builds `linux/amd64`.

On the target server, use its own kubeconfig and an already installed OPS Admin
API. Save the current StatefulSet image reference for rollback:

```sh
kubectl -n openserverless get statefulset openserverless-system-api \
  -o jsonpath='{.spec.template.spec.containers[0].image}{"\n"}'
kubectl -n openserverless get statefulset openserverless-system-api \
  -o jsonpath='{.spec.template.spec.serviceAccountName}{"\n"}'
```

Obtain `deploy/user-provisioning/rbac.yaml` from the same candidate commit.
If necessary, adjust its RoleBinding subject to the service account printed
above, then apply it. The candidate requires the same CouchDB and Kubernetes
configuration as the existing Admin API; retain that pod configuration.

```sh
kubectl apply -f deploy/user-provisioning/rbac.yaml
provisioning_container=$(kubectl -n openserverless get statefulset openserverless-system-api \
  -o jsonpath='{.spec.template.spec.containers[0].name}')
# Replace this placeholder with the published digest from the Actions summary.
provisioning_image='ghcr.io/miki3421/openserverless-admin-api@sha256:REPLACE_WITH_CANDIDATE_DIGEST'
kubectl -n openserverless set image statefulset/openserverless-system-api \
  "${provisioning_container}=${provisioning_image}"
kubectl -n openserverless rollout status statefulset/openserverless-system-api --timeout=5m
```

Create the external authorization Secret as described above and call
`POST https://<your-apihost>/system/api/v1/users` using that Secret's token as a
Bearer credential. Use a new synthetic user name. If the endpoint returns 404,
check the installed image and that the existing ingress forwards `/system` to
the Admin API. A 503 can mean the optional Secret has not been created yet;
its JSON message distinguishes that case from route setup failures. The target
host must be nginx or Traefik as documented above.

To roll back the executable, set the same container image back to the saved
reference and wait for rollout. Removing the provisioning Secret disables new
provisioning requests; neither operation deletes users already created by the
API. Provisioned users and any failed partial provisioning require separate
administrator review.
