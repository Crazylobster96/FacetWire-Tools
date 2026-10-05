# SPDX-License-Identifier: Apache-2.0
"""Trusted standalone host profile for synthetic package worksets."""
import base64
import hashlib
import io
import json
import unittest

from facetwire_tools.cli import main
from facetwire_tools.cli_host import CLIHost, settings
from facetwire_tools.descriptor import DocumentError, encode
from fwtools_tests.test_cli_host import CLIHostTests
from fwtools_tests.test_package_workspace import PackageWorkspaceTests
from fwtools_tests.test_extension_content import schema


class PackageCLITests(unittest.TestCase):
    def setUp(self):
        host=CLIHostTests();host.setUp();self.addCleanup(host.doCleanups)
        package=PackageWorkspaceTests();package.setUp();self.addCleanup(package.doCleanups)
        self.fixture,self.package=host,package
        self.export_root=host.root/"package-exports";self.export_root.mkdir()
        host.config.update(profile="managed-package-tools-v1",document_id="root",package_path=str(package.root),
                           export_root=str(self.export_root),package_limits=[8,65536,3,65536],
                           max_resource_bytes=1024,max_generations=4,allow_export=True)
        host.rewrite()

    def call(self, host, name, **arguments):
        return host.call(encode(dict(tool="facetwire."+name,arguments=arguments)))

    def test_package_cli_initialize_edit_save_export_and_reconcile(self):
        host=CLIHost(self.fixture.path)
        self.assertEqual("initialized_package",host.initialize_package()["status"])
        self.assertEqual(18,len(host.describe()["tools"]))
        located=self.call(host,"package.targets",expected_revision=1,target_id="picture",max_matches=2)
        self.assertEqual("unique",located["status"])
        self.assertEqual("documents/child.agscene/child.agscene.dis.json",located["targets"][0]["descriptor_path"])
        original=json.loads(self.call(host,"package_source.snapshot",expected_source_revision=1)["descriptor_utf8"])
        entry=next(item for item in original["files"] if item["path"]=="resources/note.txt")
        self.assertEqual(b"synthetic note",base64.b64decode(entry["base64"]))
        operation=dict(path="resources/note.txt",expected_digest=hashlib.sha256(b"synthetic note").hexdigest(),
                       base64=base64.b64encode(b"new synthetic note").decode("ascii"),scope="definition_all_instances")
        self.assertFalse(self.call(host,"package.preview_patch",expected_revision=1,expected_head=1,
                                   operations=[operation])["persisted"])
        candidate=self.call(host,"package.preview_patch",expected_revision=1,expected_head=1,
                            operations=[operation],include_candidate=True)
        self.assertFalse(candidate["persisted"])
        self.assertEqual(b"new synthetic note",base64.b64decode(next(item["base64"] for item in
            json.loads(candidate["candidate_utf8"])["files"] if item["path"]=="resources/note.txt")))
        self.call(host,"package.apply_patch",operation_id="edit",expected_revision=1,expected_head=1,
                  operations=[operation])
        self.call(host,"package.undo",operation_id="undo",expected_revision=2,expected_head=2,count=1)
        self.call(host,"package.redo",operation_id="redo",expected_revision=3,expected_head=1,count=1)
        self.call(host,"package.save",operation_id="save",expected_revision=4,expected_head=2,
                  expected_source_revision=1)
        host=CLIHost(self.fixture.path)
        source=self.call(host,"package_source.snapshot",expected_source_revision=2)
        raw=source["descriptor_utf8"].encode("utf-8")
        workset=host._open(False)[0].editor.inspect(raw)["workset_digest"]
        args=dict(operation_id="export",expected_source_revision=2,expected_source_digest=source["digest"],
                  expected_workset_digest=workset)
        self.assertFalse(self.call(host,"package.export_reconcile",**args)["recorded"])
        exported=self.call(host,"package.export",**args)
        self.assertTrue(exported["recorded"])
        self.assertTrue(self.call(host,"package.export_reconcile",**args)["recorded"])
        self.assertEqual(b"new synthetic note",(self.export_root/exported["generation"]/"example.agscene"/
                                                "resources/note.txt").read_bytes())
        self.assertEqual(b"synthetic note",(self.package.root/"resources/note.txt").read_bytes())
        self.call(host,"package.reload",expected_source_revision=2,expected_revision=5,expected_head=2,
                  operation_id="reload",dirty_policy="reject",approval_id=None)

    def test_package_profile_config_and_export_policy_default_closed(self):
        host=CLIHost(self.fixture.path)
        with self.assertRaises(DocumentError):host.initialize()
        with self.assertRaises(DocumentError):host.initialize_document(b"{}")
        self.assertEqual(0,main(["--config",str(self.fixture.path),"initialize-package"],
                                stdin=io.BytesIO(),stdout=io.BytesIO()))
        self.fixture.rewrite(allow_export=False)
        host=CLIHost(self.fixture.path)
        self.assertEqual(17,len(host.describe()["tools"]))
        self.assertFalse(self.call(host,"package.export_reconcile",operation_id="export",
            expected_source_revision=1,expected_source_digest="0"*64,expected_workset_digest="0"*64)["recorded"])
        with self.assertRaises(DocumentError):self.call(host,"package.export",operation_id="export",
            expected_source_revision=1,expected_source_digest="0"*64,expected_workset_digest="0"*64)
        self.fixture.rewrite(allow_export=True)
        self.assertEqual(18,len(CLIHost(self.fixture.path).describe()["tools"]))
        self.fixture.rewrite(document_id="wrong")
        with self.assertRaises(DocumentError):CLIHost(self.fixture.path)._open(False)
        for changed in (dict(package_path="relative.agscene"),
                        dict(export_root="relative"),dict(package_limits=[]),dict(max_resource_bytes=-1),
                        dict(max_generations=0),dict(allow_export="yes"),dict(extra=True)):
            self.fixture.rewrite(**changed)
            with self.assertRaises(DocumentError):settings(self.fixture.path)
        self.fixture.rewrite()
        self.assertEqual("managed-package-tools-v1",settings(self.fixture.path)["profile"])
        for field in ("package_path","export_root","package_limits","max_resource_bytes","max_generations","allow_export"):
            self.fixture.config.pop(field)
        self.fixture.config["profile"]="managed-local-tools-v1"
        self.fixture.rewrite()
        with self.assertRaises(DocumentError):CLIHost(self.fixture.path).initialize_package()

    def test_v2_installed_extension_is_pinned_across_edit_save_and_export(self):
        self.package.child['canvas']['pages'][0]['layers'][0]['zones'][-1]['content']={
            'type':'fact-card','body':'Synthetic custom zone','ink':'#112233ff'}
        self.package.save()
        with self.assertRaises(DocumentError):CLIHost(self.fixture.path).initialize_package()
        profile_file=self.fixture.root/'fact-card.schema.json'
        profile_file.write_bytes(schema())
        entry=dict(type='fact-card',schema_path=str(profile_file),sha256=hashlib.sha256(schema()).hexdigest())
        for invalid in ([],[dict(entry,extra=True)]):
            self.fixture.rewrite(profile='managed-package-tools-v2',extension_schemas=invalid)
            with self.assertRaises(DocumentError):settings(self.fixture.path)
        self.fixture.rewrite(profile='managed-package-tools-v2',extension_schemas=[entry],
                             database_path=str(self.fixture.root/'extension.db'))
        host=CLIHost(self.fixture.path)
        self.assertEqual('initialized_package',host.initialize_package()['status'])
        self.assertEqual(18,len(host.describe()['tools']))
        source=self.call(host,'package_source.snapshot',expected_source_revision=1)
        target=self.call(host,'package.targets',expected_revision=1,target_id='picture',max_matches=2)
        self.assertEqual('unique',target['status'])
        self.call(host,'package.apply_patch',operation_id='custom-edit',expected_revision=1,expected_head=1,
                  operations=[dict(descriptor_path='example.agscene.dis.json',
                      expected_descriptor_digest=hashlib.sha256(encode(self.package.parent)).hexdigest(),
                      target_id='root',field='title',expected='Synthetic CLI',value='Custom title',
                      scope='definition_all_instances')])
        self.call(host,'package.save',operation_id='custom-save',expected_revision=2,expected_head=2,
                  expected_source_revision=1)
        host=CLIHost(self.fixture.path)
        saved=self.call(host,'package_source.snapshot',expected_source_revision=2)
        workset=host._open(False)[0].editor.inspect(saved['descriptor_utf8'].encode('utf-8'))['workset_digest']
        exported=self.call(host,'package.export',operation_id='custom-export',expected_source_revision=2,
                           expected_source_digest=saved['digest'],expected_workset_digest=workset)
        self.assertTrue(exported['recorded'])
        profile_file.write_bytes(b'changed')
        with self.assertRaises(DocumentError):host._policy()
        with self.assertRaises(DocumentError):CLIHost(self.fixture.path).describe()
        self.assertNotEqual(source['digest'],saved['digest'])
