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
import tempfile
import subprocess
from pathlib import Path
from unittest.mock import Mock

from openserverless.impl.builder.build_service import BuildService


class BuildTargetSecurityTest(unittest.TestCase):
    def test_rejects_shell_syntax_before_kubernetes_calls(self):
        service = BuildService.__new__(BuildService)
        service.user = "alpha"
        service.kube_client = Mock()
        for target in ("alpha:ok;id", "alpha:ok$(id)", "alpha:ok\nRUN id", "alpha:ok,foo=bar"):
            self.assertEqual((False, "Invalid target for the build"), service.build(target))
        service.kube_client.assert_not_called()
        self.assertEqual([], service.kube_client.mock_calls)

    def test_valid_target_is_passed_as_data_to_buildctl(self):
        service = BuildService.__new__(BuildService)
        service.user = "alpha"
        service.registry_host = "registry.local:5000"
        service.custom_registry_auth = False
        service.job_name = "build-alpha-test"
        service.cm = "cm-alpha-test"
        service.registry_auth = "registry-pull-secret"
        job = service.create_build_job("alpha:release_1.2")
        buildkit = job["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual("registry.local:5000/alpha:release_1.2", next(x["value"] for x in buildkit["env"] if x["name"] == "REGISTRY_IMAGE_NAME"))
        self.assertIn('${REGISTRY_IMAGE_NAME}', buildkit["args"][0])
        self.assertNotIn("alpha:release_1.2", buildkit["args"][0])
        pod = job["spec"]["template"]["spec"]
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertFalse(pod["hostUsers"])
        self.assertEqual("Unmasked", buildkit["securityContext"]["procMount"])
        self.assertIn("@sha256:", buildkit["image"])
        self.assertEqual({"/etc/subuid", "/etc/subgid"}, {
            mount["mountPath"] for mount in buildkit["volumeMounts"] if mount.get("subPath")
        })
        self.assertFalse(buildkit["securityContext"]["privileged"])
        self.assertEqual("Unconfined", buildkit["securityContext"]["appArmorProfile"]["type"])
        self.assertEqual("Unconfined", buildkit["securityContext"]["seccompProfile"]["type"])

    def test_source_cannot_add_dockerfile_instructions(self):
        self.assertTrue(BuildService.valid_source_image("docker.io/apache/runtime:1.2"))
        self.assertFalse(BuildService.valid_source_image("docker.io/apache/runtime:1.2\nRUN id"))
        self.assertFalse(BuildService.valid_image_target("beta:release", "alpha"))

    def test_python_requirements_support_standard_and_extension_aware_images(self):
        service = BuildService.__new__(BuildService)
        service.build_config = {"source": "runtime:python", "kind": "python", "file": "encoded"}
        dockerfile = service.create_docker_file("requirements.txt")
        self.assertIn("COPY requirements.txt /tmp/requirements.txt\n", dockerfile)
        self.assertIn("if [ -x /bin/extend ]; then /bin/extend;", dockerfile)
        self.assertIn("python -m pip install --no-cache-dir -r /tmp/requirements.txt", dockerfile)
        self.assertTrue(dockerfile.endswith("USER nobody\n"))

    def test_other_languages_keep_extension_contract(self):
        service = BuildService.__new__(BuildService)
        service.build_config = {"source": "runtime:php", "kind": "php", "file": "encoded"}
        dockerfile = service.create_docker_file("composer.json")
        self.assertIn("RUN /bin/extend\n", dockerfile)
        self.assertNotIn("pip install", dockerfile)

    def test_node_dependencies_use_separate_module_directory(self):
        service = BuildService.__new__(BuildService)
        service.build_config = {"source": "runtime:node", "kind": "nodejs", "file": "encoded"}
        dockerfile = service.create_docker_file("package.json")
        self.assertIn("if [ -x /bin/extend ]; then /bin/extend;", dockerfile)
        self.assertIn("npm install --prefix /opt/ops-deps --omit=dev", dockerfile)
        self.assertIn("ln -s /opt/ops-deps/node_modules /app/node_modules", dockerfile)
        self.assertTrue(dockerfile.endswith("USER nobody\n"))

    def test_node_fallback_refuses_existing_directory_or_dangling_link(self):
        service = BuildService.__new__(BuildService)
        service.build_config = {"source": "runtime:node", "kind": "nodejs", "file": "encoded"}
        dockerfile = service.create_docker_file("package.json")
        command = next(line[4:] for line in dockerfile.splitlines() if line.startswith("RUN "))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "node_modules"
            # If the guard fails, no npm command or filesystem mutation may run.
            command = command.replace("/bin/extend", str(root / "absent-hook"))
            command = command.replace("/app/node_modules", str(target))
            target.mkdir()
            for kind in ("directory", "dangling-link"):
                if kind == "dangling-link":
                    target.rmdir()
                    target.symlink_to(root / "absent-target")
                result = subprocess.run(["sh", "-c", command], capture_output=True)
                self.assertNotEqual(0, result.returncode)
                if kind == "directory":
                    self.assertEqual([], list(target.iterdir()))
                else:
                    self.assertTrue(target.is_symlink())

    def test_base_image_only_build_does_not_install_dependencies(self):
        service = BuildService.__new__(BuildService)
        service.build_config = {"source": "runtime:python", "kind": "python"}
        self.assertEqual("FROM runtime:python\n\n", service.create_docker_file())


if __name__ == "__main__":
    unittest.main()
