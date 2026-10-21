# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
#

import base64
import copy
from concurrent.futures import ThreadPoolExecutor
import io
import logging
import threading
import unittest
from unittest.mock import Mock, patch

import requests
from openserverless import app
from openserverless.impl.provisioning.kube_client import ProvisioningError, ProvisioningKubeClient
from openserverless.impl.provisioning.routes import route_context, tenant_routes
from openserverless.impl.provisioning.user_service import UserProvisioningService, SERVICE_NAMES

TOKEN = "synthetic-provisioning-token-only"
BODY = {"user": "labtenant", "password": "Synthetic-pass!42", "email": "lab@example.test"}


class FakeKube:
    def __init__(self, components=None):
        self.secret = {"type": "Opaque", "data": {"token": base64.b64encode(TOKEN.encode()).decode()}}
        self.configuration = {"spec": {"components": components if components is not None else
                                       {name: True for name in SERVICE_NAMES if name != "minio"}}}
        self.ingress = {"spec": {"ingressClassName": "traefik", "rules": [{"host": "example.test"}]}}
        self.users = {}
        self.routes = []
        self.lock = threading.Lock()
        self.secret_reads = 0

    def provisioning_secret(self):
        self.secret_reads += 1
        return self.secret

    def existing_user(self, name):
        return copy.deepcopy(self.users.get(name))

    def cluster(self): return self.configuration
    def apihost_ingress(self): return self.ingress
    def streamer_service(self): return {"metadata": {"name": "openserverless-streamer-api"}}

    def create_user(self, body):
        with self.lock:
            name = body["metadata"]["name"]
            if name in self.users:
                raise ProvisioningError("Resource already exists", 409)
            result = copy.deepcopy(body)
            result["metadata"]["uid"] = f"synthetic-{name}-uid"
            self.users[name] = result
            return copy.deepcopy(result)

    def create_route(self, body): self.routes.append(copy.deepcopy(body))


class ProvisioningApiTest(unittest.TestCase):
    def setUp(self):
        self.kube = FakeKube()
        self.factory = patch("openserverless.rest.users.ProvisioningKubeClient", return_value=self.kube)
        self.factory.start()
        self.addCleanup(self.factory.stop)

    def post(self, body=BODY, token=TOKEN, **kwargs):
        headers = {} if token is None else {"Authorization": "Bearer " + token}
        with app.test_client() as client:
            return client.post("/system/api/v1/users", headers=headers, json=body, **kwargs)

    def test_defaults_create_full_tenant_and_owned_routes_without_credentials_in_response(self):
        response = self.post()
        self.assertEqual(202, response.status_code)
        self.assertEqual("pending", response.json["provisioning_status"])
        self.assertEqual([s for s in SERVICE_NAMES if s != "minio"], response.json["services"])
        resource = self.kube.users[BODY["user"]]
        spec = resource["spec"]
        self.assertEqual(BODY["password"], spec["password"])
        self.assertNotIn("annotations", resource["metadata"])
        self.assertEqual("labtenant", spec["mongodb"]["database"])
        self.assertEqual("labtenant", spec["redis"]["prefix"])
        self.assertEqual("auto", spec["object-storage"]["quota"])
        self.assertTrue(spec["object-storage"]["data"]["enabled"])
        self.assertTrue(spec["object-storage"]["route"]["enabled"])
        self.assertEqual(6, len(self.kube.routes))
        for route in self.kube.routes:
            self.assertEqual(resource["metadata"]["uid"], route["metadata"]["ownerReferences"][0]["uid"])
        for secret in (TOKEN, spec["auth"], spec["password"], spec["redis"]["password"]):
            self.assertNotIn(secret, response.get_data(as_text=True))

    def test_slim_defaults_enable_only_installed_services(self):
        self.kube.configuration["spec"]["components"] = {"redis": True, "seaweedfs": True}
        response = self.post()
        self.assertEqual(202, response.status_code)
        self.assertEqual(["redis", "seaweedfs"], response.json["services"])
        self.assertNotIn("mongodb", self.kube.users["labtenant"]["spec"])

    def test_explicit_subset_and_per_request_credentials_are_isolated(self):
        self.assertEqual(202, self.post({**BODY, "services": "postgres, redis"}).status_code)
        second = self.post({**BODY, "user": "seconduser", "services": "mongodb"})
        self.assertEqual(202, second.status_code)
        first = self.kube.users["labtenant"]["spec"]
        other = self.kube.users["seconduser"]["spec"]
        self.assertNotIn("mongodb", first)
        self.assertNotIn("redis", other)
        self.assertNotEqual(first["auth"], other["auth"])
        self.assertNotIn("object-storage", first)

    def test_unavailable_unknown_or_ambiguous_services_never_create(self):
        self.kube.configuration["spec"]["components"] = {"redis": True}
        for services in ("mongodb", "unrecognized", "", "all,redis", "redis,redis", "redis,", None, [], "minio"):
            with self.subTest(services=services):
                self.assertEqual(400, self.post({**BODY, "services": services}).status_code)
        self.assertEqual({}, self.kube.users)
        self.assertEqual([], self.kube.routes)

    def test_backend_name_limits_fail_before_creation(self):
        for name, services in (("a" * 55, "mongodb"), ("a" * 59, "seaweedfs")):
            self.assertEqual(400, self.post({**BODY, "user": name, "services": services}).status_code)
        self.assertEqual({}, self.kube.users)
        self.assertEqual(202, self.post({**BODY, "user": "a" * 61, "services": "redis"}).status_code)

    def test_no_auth_or_invalid_auth_never_checks_or_creates_users(self):
        with patch.object(self.kube, "existing_user") as lookup:
            for token in (None, "wrong-token", "namespace-uuid:tenant-key"):
                self.assertEqual(401, self.post(token=token).status_code)
            for header in ("Basic abc", "Bearer", "Bearer a b", "Bearer \u00e9", "Bearer " + "a" * 8193):
                with app.test_client() as client:
                    self.assertEqual(401, client.post("/system/api/v1/users", headers={"Authorization": header}, json=BODY).status_code)
            lookup.assert_not_called()
        self.assertEqual({}, self.kube.users)

    def test_missing_malformed_and_unreadable_secret_fail_closed(self):
        for secret in (None, [], {}, {"type": "Opaque", "data": {}}, {"type": "Opaque", "data": {"token": "%%%"}},
                       {"type": "Opaque", "data": {"token": ""}}, {"type": "Opaque", "data": {"token": "IA=="}},
                       {"type": "kubernetes.io/tls", "data": {"token": "dG9rZW4="}}):
            self.kube.secret = secret
            self.assertEqual(503, self.post().status_code)
        with patch.object(self.kube, "provisioning_secret", side_effect=ProvisioningError("dependency unavailable")):
            self.assertEqual(503, self.post().status_code)
        self.assertEqual({}, self.kube.users)

    def test_rotation_revocation_without_restart(self):
        self.assertEqual(202, self.post().status_code)
        self.kube.secret["data"]["token"] = base64.b64encode(b"rotated-synthetic").decode()
        self.assertEqual(401, self.post({**BODY, "user": "seconduser"}).status_code)
        self.assertEqual(202, self.post({**BODY, "user": "seconduser"}, token="rotated-synthetic").status_code)
        self.kube.secret = None
        self.assertEqual(503, self.post({**BODY, "user": "thirduser"}, token="rotated-synthetic").status_code)
        self.assertEqual(4, self.kube.secret_reads)

    def test_existing_blocked_deleting_users_are_unchanged(self):
        self.post()
        self.kube.users["labtenant"]["spec"]["blocked"] = True
        self.kube.users["labtenant"]["metadata"]["deletionTimestamp"] = "2026-10-21T00:00:00Z"
        before = copy.deepcopy(self.kube.users)
        routes = copy.deepcopy(self.kube.routes)
        self.assertEqual(409, self.post({**BODY, "password": "different", "services": "redis"}).status_code)
        self.assertEqual(before, self.kube.users)
        self.assertEqual(routes, self.kube.routes)

    def test_concurrent_creation_returns_one_acceptance_one_conflict(self):
        barrier = threading.Barrier(2)
        def lookup(name):
            barrier.wait(timeout=5)
            return None
        with patch.object(self.kube, "existing_user", side_effect=lookup):
            with ThreadPoolExecutor(max_workers=2) as executor:
                responses = list(executor.map(lambda _: self.post().status_code, range(2)))
        self.assertEqual([202, 409], sorted(responses))
        self.assertEqual(1, len(self.kube.users))
        self.assertEqual(6, len(self.kube.routes))

    def test_validation_payload_types_unknown_fields_and_no_normalization(self):
        invalid = [None, [], "text", {}, {**BODY, "role": "admin"}, {**BODY, "token": TOKEN}]
        for field in ("user", "password", "email"):
            invalid += [{k: v for k, v in BODY.items() if k != field}]
            invalid += [{**BODY, field: value} for value in (None, "", " ", 123, True, [], "a\nb")]
        invalid += [{**BODY, "user": value} for value in ("ABcde", "abc", "a-bcd", "a/bcd", "openserverless", "a" * 62)]
        invalid += [{**BODY, "email": value} for value in ("a@b", "a b@example.test", "a@@b.test", 'a"@example.test', 'a\\b@example.test', "a..b@example.test", "a@-example.test", "a@exa_mple.test")]
        invalid += [{**BODY, "password": "\ud800"}, {**BODY, "email": "\ud800@example.test"}, {**BODY, "password": "x" * 73}, {**BODY, "password": "\u00e9" * 37}]
        with app.test_client() as client:
            for body in invalid:
                with self.subTest(body_type=type(body).__name__):
                    # data=json is explicit here because Flask json=None omits the body.
                    import json
                    response = client.post("/system/api/v1/users", data=json.dumps(body), content_type="application/json",
                                           headers={"Authorization": "Bearer " + TOKEN})
                    self.assertEqual(400, response.status_code)
        self.assertEqual({}, self.kube.users)

    def test_content_type_json_errors_body_limit_and_auth_first(self):
        with app.test_client() as client:
            for data, content_type, code in (("{", "application/json", 400), ("{}", "text/plain", 415),
                                             (" " * 16385, "application/json", 413)):
                response = client.post("/system/api/v1/users", data=data, content_type=content_type,
                                       headers={"Authorization": "Bearer " + TOKEN})
                self.assertEqual(code, response.status_code)
            self.assertEqual(401, client.post("/system/api/v1/users", data="{", content_type="text/plain").status_code)

    def test_dependency_lookup_failure_is_not_treated_as_absent(self):
        with patch.object(self.kube, "existing_user", side_effect=ProvisioningError("dependency unavailable")):
            self.assertEqual(503, self.post().status_code)
        self.assertEqual({}, self.kube.users)

    def test_partial_route_failure_is_explicit_and_does_not_delete_user(self):
        with patch.object(self.kube, "create_route", side_effect=ProvisioningError("dependency unavailable")):
            response = self.post()
        self.assertEqual(503, response.status_code)
        self.assertIn("User created but route setup failed", response.json["message"])
        self.assertIn("labtenant", self.kube.users)
        self.assertEqual(409, self.post().status_code)

    def test_config_and_ingress_failure_no_user_created(self):
        for config in ({}, {"spec": {"components": {"redis": "false"}}}):
            self.kube.configuration = config
            self.assertEqual(503, self.post().status_code)
        self.kube.configuration = {"spec": {"components": {}}}
        self.kube.ingress["spec"]["ingressClassName"] = "unsupported"
        self.assertEqual(503, self.post().status_code)
        self.assertEqual({}, self.kube.users)

    def test_no_sensitive_error_or_request_logs(self):
        log = io.StringIO()
        handler = logging.StreamHandler(log)
        logging.getLogger().addHandler(handler)
        self.addCleanup(logging.getLogger().removeHandler, handler)
        with patch.object(self.kube, "create_user", side_effect=RuntimeError(BODY["password"] + TOKEN)):
            response = self.post()
        self.assertEqual(500, response.status_code)
        for value in (TOKEN, BODY["password"]):
            self.assertNotIn(value, log.getvalue() + response.get_data(as_text=True))

    def test_spec_exposes_contract_without_real_credentials(self):
        with app.test_client() as client:
            response = client.get("/system/apispec_1.json")
        self.assertEqual(200, response.status_code)
        spec = response.json
        self.assertIn("/system/api/v1/users", spec["paths"])
        self.assertIn("provisioningBearer", spec["securityDefinitions"])


class ProvisioningKubeClientTest(unittest.TestCase):
    def setUp(self):
        self.client = ProvisioningKubeClient.__new__(ProvisioningKubeClient)
        self.client.host = "https://kubernetes.test"
        self.client.token = "Bearer synthetic-service-account"
        self.client.ssl_ca_cert = "/synthetic/ca.crt"

    @patch("openserverless.impl.provisioning.kube_client.requests.request")
    def test_missing_vs_forbidden_error_conflict_json_and_transport(self, request):
        response = request.return_value
        response.status_code = 404
        self.assertIsNone(self.client.existing_user("validuser"))
        for code in (401, 403, 500, 503):
            response.status_code = code
            with self.assertRaises(ProvisioningError) as error: self.client.existing_user("validuser")
            self.assertEqual(503, error.exception.status)
        response.status_code = 409
        with self.assertRaises(ProvisioningError) as error: self.client.create_user({})
        self.assertEqual(409, error.exception.status)
        response.status_code = 201
        response.json.return_value = {"metadata": {"uid": "uid"}}
        self.assertEqual("uid", self.client.create_user({})["metadata"]["uid"])
        self.assertEqual((5, 20), request.call_args.kwargs["timeout"])
        self.assertEqual("/synthetic/ca.crt", request.call_args.kwargs["verify"])
        response.json.side_effect = ValueError("sensitive response")
        with self.assertRaises(ProvisioningError): self.client.create_user({})
        request.side_effect = requests.Timeout("sensitive request")
        with self.assertRaises(ProvisioningError) as error: self.client.create_user({})
        self.assertNotIn("sensitive", str(error.exception))


class TenantRouteTest(unittest.TestCase):
    def test_nginx_traefik_tls_ownership_and_sigv4_path_contracts(self):
        for ingress_class in ("nginx", "traefik"):
            for tls in (True, False):
                routes = tenant_routes("labtenant", (ingress_class, "example.test", tls), ["seaweedfs"], True, "owner-uid")
                ingresses = [r for r in routes if r["kind"] == "Ingress"]
                self.assertEqual(4, len(ingresses))
                for ingress in ingresses:
                    self.assertEqual(tls, bool(ingress["spec"].get("tls")))
                    self.assertEqual("owner-uid", ingress["metadata"]["ownerReferences"][0]["uid"])
                s3 = ingresses[-1]
                self.assertEqual(["/labtenant-web", "/labtenant-data"], [p["path"] for p in s3["spec"]["rules"][0]["http"]["paths"]])
                self.assertFalse(any("rewrite" in key or "middlewares" in key for key in s3["metadata"]["annotations"]))
                serialized = str(routes)
                self.assertIn("/web/labtenant/$2", serialized)
                self.assertIn("/action/labtenant/$2", serialized)
        self.assertEqual([], tenant_routes("labtenant", ("nginx", "example.test", False), ["redis"], False, "uid"))


if __name__ == "__main__": unittest.main()
