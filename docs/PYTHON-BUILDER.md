<!-- Licensed to the Apache Software Foundation (ASF) under one or more
contributor license agreements. See the NOTICE file distributed with this
work for additional information regarding copyright ownership. The ASF
licenses this file to You under the Apache License, Version 2.0 (the
"License"); you may not use this file except in compliance with the License.
You may obtain a copy of the License at http://www.apache.org/licenses/LICENSE-2.0
Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
License for the specific language governing permissions and limitations
under the License. -->

# Python builds for the custom task fork

This branch keeps the existing `/system/api/v1/build/start` request and response
contract. Python builds now execute `python -m pip install -r
/tmp/requirements.txt` as root and return to `USER nobody`, matching the
application's dependency image. Other language builders keep `/bin/extend`.
The Dockerfile now includes the default BuildKit configuration so that the API
can create its configuration ConfigMap when it is absent.

The companion tasks are in `miki3421/openserverless-task-custom`, branch
`codex/ide-build`. They wait for the exact Job and verify the registry manifest
before deploying an action. Builds still use native cluster architecture, the
internal registry, and namespace-owned repository names.

The existing `Build Image` GitHub workflow supports manual builds of this
branch and publishes fork images under `ghcr.io/miki3421/openserverless-admin-api`.
Run it manually when ready:

```bash
gh workflow run image.yml --repo miki3421/openserverless-admin-api --ref codex/python-build
```

Use the concrete published tag/digest in the server deployment. If GHCR is not
reachable from the client server, transfer an OCI image archive or publish to a
registry that the server can reach. Updating task sources alone does not update
the running System API image.

Automated tests, image builds, and server deployment have not been run for this
change. The existing API build Jobs' runtime prerequisites and registry
configuration must be checked in staging before production.
