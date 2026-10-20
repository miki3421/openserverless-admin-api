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
import json
import unittest
from unittest.mock import Mock, patch

from openserverless import app
from openserverless.couchdb.couchdb_util import CouchDB
from openserverless.common.openwhisk_authorize import OpenwhiskAuthorize
from openserverless.error.api_error import AuthorizationError
from openserverless.impl.auth.auth_service import AuthService


class SubjectDatabase(CouchDB):
    def __init__(self):
        self.subject = {'subject': 'tenant', 'namespaces': [{'name': 'tenant', 'uuid': 'uuid', 'key': 'key'}]}
        self.metadata = {'login': 'tenant', 'email': 'test@example.test', 'password': 'hash',
                         'env': [{'key': 'AUTH', 'value': 'uuid:key'}]}

    def find_doc(self, database, selector):
        if database == 'subjects': return {'docs': [self.subject] if self.subject else []}
        return {'docs': [copy.deepcopy(self.metadata)]}


class SubjectRevocationTest(unittest.TestCase):
    def setUp(self):
        self.db = SubjectDatabase()
        self.kube = Mock()
        self.claims = {'iss': 'https://issuer.test', 'sub': 'owner', 'preferred_username': 'tenant'}
        self.kube.get_whisk_user.return_value = {'metadata': {'uid': 'stable-uid', 'annotations': {
            'openserverless.apache.org/sso-issuer': self.claims['iss'],
            'openserverless.apache.org/sso-sub': self.claims['sub'],
        }}, 'spec': {'namespace': 'tenant', 'auth': 'uuid:key'}}
        self.service = AuthService(environ={}, couch_db=self.db, kube_client=self.kube)

    def test_block_unblock_delete_on_api_key_password_oidc_and_password_update(self):
        helper = OpenwhiskAuthorize.__new__(OpenwhiskAuthorize); helper._db = self.db
        for state in ('active', 'blocked', 'unblocked', 'deleted'):
            with self.subTest(state=state):
                if state == 'deleted': self.db.subject = None
                else: self.db.subject['blocked'] = state == 'blocked'
                allowed = state in ('active', 'unblocked')
                for token in ('uuid:key', 'Basic ' + base64.b64encode(b'uuid:key').decode()):
                    for method in (helper.login, helper.subject_login):
                        if allowed: self.assertTrue(method(token))
                        else:
                            with self.assertRaises(AuthorizationError): method(token)
                with app.app_context(), patch('openserverless.impl.auth.auth_service.bu.verify_password', return_value=True):
                    response = self.service.login('tenant', 'password')
                    self.assertEqual(200 if allowed else 401, response.status_code)
                    self.kube.reset_mock()
                    response = self.service.update_password('tenant', 'old', 'new')
                    self.assertEqual(200 if allowed else 401, response.status_code)
                    self.assertEqual(int(allowed), self.kube.update_whisk_user.call_count)
                with app.app_context(), patch('openserverless.impl.auth.auth_service.OidcTokenValidator') as validator:
                    validator.return_value.validate.return_value = self.claims
                    response = self.service.login_oidc('signed-token')
                    self.assertEqual(200 if allowed else 403, response.status_code)
                    if not allowed: self.assertNotIn('AUTH', response.json)

    def test_empty_or_removed_namespace_does_not_leave_password_access(self):
        self.db.subject['namespaces'] = []
        with app.app_context(), patch('openserverless.impl.auth.auth_service.bu.verify_password', return_value=True):
            self.assertEqual(401, self.service.login('tenant', 'password').status_code)

    def test_backend_errors_fail_closed_without_modifying_tenant(self):
        with patch.object(self.db, 'find_active_subject', side_effect=RuntimeError('database unavailable')):
            with app.app_context(), patch('openserverless.impl.auth.auth_service.bu.verify_password', return_value=True):
                self.assertEqual(401, self.service.login('tenant', 'password').status_code)
                self.assertEqual(401, self.service.update_password('tenant', 'old', 'new').status_code)
        self.kube.update_whisk_user.assert_not_called()

    def test_wrong_credentials_and_other_tenant_cannot_match(self):
        for arguments in ({'uuid': 'uuid', 'key': 'wrong'}, {'subject': 'victim'},
                          {'uuid': 'uuid', 'key': 'key', 'subject': 'victim'}, {},
                          {'subject': {'$ne': None}}):
            self.assertIsNone(self.db.find_active_subject(**arguments))

    def test_missing_admin_password_never_uses_a_fallback(self):
        for value in (None, '', ' '):
            with self.assertRaises(ValueError):
                CouchDB(environ={} if value is None else {'COUCHDB_ADMIN_PASSWORD': value})
        self.assertEqual('synthetic-only', CouchDB(environ={'COUCHDB_ADMIN_PASSWORD': 'synthetic-only'}).db_password)


if __name__ == '__main__': unittest.main()
