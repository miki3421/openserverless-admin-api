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

import unittest
from unittest.mock import patch
from openserverless import app
from openserverless.rest import build
from openserverless.impl.builder.build_service import BuildService

class BuildStatusTest(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        self.job = {"metadata": {"name": "build-alpha-test", "labels": {
            "ops.openserverless.org/build-owner": BuildService.owner_label("alpha")},
            "annotations": {"ops.openserverless.org/build-image": "registry/alpha:test"}},
            "status": {"succeeded": 1}, "spec": {"secret": "private"}}

    def test_anonymous_is_rejected_without_kubernetes_access(self):
        with patch("openserverless.common.kube_api_client.KubeApiClient") as kube:
            self.assertEqual(401, self.client.get("/system/api/v1/build/status/build-alpha-test").status_code)
            kube.assert_not_called()

    def test_status_only_for_owner_and_without_manifest(self):
        with patch.object(build, "authorize", return_value={"login":"alpha"}), patch(
                "openserverless.common.kube_api_client.KubeApiClient") as kube:
            kube.return_value.get_build_job.return_value = self.job
            response = self.client.get("/system/api/v1/build/status/build-alpha-test")
            self.assertEqual(200, response.status_code)
            self.assertEqual("no-store", response.headers["Cache-Control"])
            self.assertEqual({"job_name":"build-alpha-test", "state":"succeeded", "image":"registry/alpha:test"}, response.json)
        with patch.object(build, "authorize", return_value={"login":"beta"}), patch(
                "openserverless.common.kube_api_client.KubeApiClient") as kube:
            kube.return_value.get_build_job.return_value = self.job
            self.assertEqual(404, self.client.get("/system/api/v1/build/status/build-alpha-test").status_code)
            kube.return_value.get_build_job.return_value = None
            self.assertEqual(404, self.client.get("/system/api/v1/build/status/build-alpha-test").status_code)

    def test_invalid_name_and_upstream_failure_are_safe(self):
        with patch.object(build, "authorize", return_value={"login":"alpha"}), patch(
                "openserverless.common.kube_api_client.KubeApiClient") as kube:
            self.assertEqual(404, self.client.get("/system/api/v1/build/status/not-a-build").status_code)
            kube.assert_not_called()
            kube.return_value.get_build_job.side_effect = RuntimeError("private data")
            response = self.client.get("/system/api/v1/build/status/build-alpha-test")
            self.assertEqual(503, response.status_code)
            self.assertNotIn("private", response.get_data(as_text=True))
