# SPDX-License-Identifier: Apache-2.0
"""New-generation materialization uses only synthetic temporary FacetWire files."""
import hashlib
import json
import unittest
import base64
from pathlib import Path
from unittest.mock import patch

from facetwire_tools.descriptor import DocumentError
from facetwire_tools.package_editor import PackageEditor
from facetwire_tools.package_export import PackageExport, _sha
from facetwire_tools.package_export_tools import PackageExportTools, definitions as export_definitions
from facetwire_tools.package_workspace import PackageSnapshot, PackageWorkspace
from facetwire_tools.storage import DescriptorStore
from fwtools_tests.test_package_workspace import PackageWorkspaceTests


class PackageExportTests(unittest.TestCase):
    def setUp(self):
        fixture=PackageWorkspaceTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        self.fixture=fixture
        self.workspace=fixture.open()
        self.editor=PackageEditor(self.workspace.read(),fixture.editor,max_bytes=65536,max_resource_bytes=1024)
        db=fixture.root.parent/"export-source.db"
        self.store=DescriptorStore(db,self.editor,scope="source",actor="actor",authorize=lambda scope:True,
                                   max_events=8,max_total_bytes=1048576,create=True)
        self.initial=self.editor.initial()
        self.store.save(self.initial,actor="actor",source_key="package",expected_revision=0,
                        operation_id="initial",mode="managed_local")
        self.export_root=fixture.root.parent/"exports";self.export_root.mkdir()
        self.exporter=PackageExport(self.workspace,self.store,actor="actor",source_key="package",
                                    export_root=self.export_root,max_generations=2)

    def request(self, raw=None, revision=1, operation_id="export-one"):
        raw=self.initial if raw is None else raw
        return dict(operation_id=operation_id,expected_source_revision=revision,
                    expected_source_digest=hashlib.sha256(raw).hexdigest(),
                    expected_workset_digest=self.editor.inspect(raw)["workset_digest"])

    def test_saved_workset_becomes_new_immutable_package_and_reconciles(self):
        request=self.request()
        self.assertFalse(self.exporter.reconcile(**request)["recorded"])
        receipt=self.exporter.export(**request)
        self.assertTrue(receipt["recorded"])
        self.assertEqual(receipt,self.exporter.reconcile(**request))
        self.assertEqual(receipt,self.exporter.export(**request))
        generated=self.export_root/receipt["generation"]/"example.agscene"
        self.assertEqual(self.workspace.read().digest,PackageWorkspace(generated,self.fixture.editor,
            max_files=8,max_total_bytes=65536,max_depth=3,max_file_bytes=65536).read().digest)
        self.assertEqual(b"synthetic note",(self.fixture.root/"resources/note.txt").read_bytes())
        self.assertEqual(b"synthetic note",(generated/"resources/note.txt").read_bytes())
        with self.assertRaises(DocumentError):self.exporter.export(**dict(request,expected_source_digest="0"*64))
        (generated/"resources/note.txt").write_bytes(b"tampered")
        with self.assertRaises(DocumentError):self.exporter.reconcile(**request)

    def test_edited_resource_and_nested_descriptor_are_materialized_from_saved_source_only(self):
        raw=self.editor.prepare_patch(self.initial,(
            dict(path="resources/note.txt",expected_digest=hashlib.sha256(b"synthetic note").hexdigest(),
                 base64="bmV3IG5vdGU=",scope="definition_all_instances"),))
        path="documents/child.agscene/child.agscene.dis.json"
        before=dict(self.editor.snapshot.files)[path]
        raw=self.editor.prepare_patch(raw,(dict(descriptor_path=path,expected_descriptor_digest=hashlib.sha256(before).hexdigest(),
            target_id="picture",field="bounds",expected={"x":0,"y":120,"width":100,"height":100},
            value={"x":15,"y":120,"width":100,"height":100},scope="definition_all_instances"),))
        self.store.save(raw,actor="actor",source_key="package",expected_revision=1,
                        operation_id="saved-edit",mode="managed_local")
        request=self.request(raw,2,"export-two")
        receipt=self.exporter.export(**request)
        generated=self.export_root/receipt["generation"]/"example.agscene"
        self.assertEqual(b"new note",(generated/"resources/note.txt").read_bytes())
        child=json.loads((generated/path).read_text(encoding="utf-8"))
        self.assertEqual(15,child["canvas"]["pages"][0]["layers"][0]["zones"][-1]["bounds"]["x"])
        self.assertEqual(b"synthetic note",(self.fixture.root/"resources/note.txt").read_bytes())

    def test_preconditions_source_changes_capacity_and_bad_export_roots(self):
        request=self.request()
        for wrong in (dict(request,expected_source_revision=0),dict(request,expected_workset_digest="0"*64),
                      dict(request,expected_source_digest="0"*64),dict(request,operation_id="bad/path")):
            with self.assertRaises(DocumentError):self.exporter.export(**wrong)
        for value in (None,0,"A"*64,"0"*63):
            with self.assertRaises(DocumentError):_sha(value)
        with self.assertRaises(DocumentError):PackageExport(self.workspace,self.store,actor="actor",source_key="package",
                                                            export_root=self.export_root/"missing",max_generations=2)
        with self.assertRaises(DocumentError):PackageExport(self.workspace,self.store,actor="actor",source_key="package",
                                                            export_root=self.export_root,max_generations=2,authorize_write=1)
        (self.fixture.root/"resources/note.txt").write_bytes(b"changed externally")
        with self.assertRaises(DocumentError):PackageExport(self.workspace,self.store,actor="actor",source_key="package",
                                                            export_root=self.export_root,max_generations=2)

    def test_capacity_and_failed_stage_do_not_claim_export(self):
        request=self.request()
        one=PackageExport(self.workspace,self.store,actor="actor",source_key="package",
                          export_root=self.export_root,max_generations=1)
        one.export(**request)
        with self.assertRaises(DocumentError):one.export(**dict(request,operation_id="another"))
        self.assertFalse(one.reconcile(**dict(request,operation_id="another"))["recorded"])
        root=self.fixture.root.parent/"failure-exports";root.mkdir()
        failure=PackageExport(self.workspace,self.store,actor="actor",source_key="package",
                              export_root=root,max_generations=1)
        with patch.object(failure.workspace,"read",side_effect=DocumentError("synthetic source changed")):
            with self.assertRaises(DocumentError):failure.export(**request)
        self.assertFalse(failure.reconcile(**request)["recorded"])
        self.assertEqual([],list(root.iterdir()))

    def test_export_generation_manifest_links_and_extra_files_fail_closed(self):
        request=self.request()
        folder=self.exporter.export(**request)["generation"]
        destination=self.export_root/folder
        manifest=destination/"manifest.json"
        original=manifest.read_bytes()
        manifest.write_bytes(b"{}");
        with self.assertRaises(DocumentError):self.exporter.reconcile(**request)
        manifest.write_bytes(original)
        (destination/"example.agscene"/"unreferenced.bin").write_bytes(b"extra")
        with self.assertRaises(DocumentError):self.exporter.reconcile(**request)
        (destination/"example.agscene"/"unreferenced.bin").unlink()
        (destination/"example.agscene"/"extra").mkdir()
        with self.assertRaises(DocumentError):self.exporter.reconcile(**request)
        (destination/"example.agscene"/"extra").rmdir()
        self.assertTrue(self.exporter.reconcile(**request)["recorded"])
        original_symlink=Path.is_symlink
        with patch.object(Path,"is_symlink",lambda path:path==destination or original_symlink(path)):
            with self.assertRaises(DocumentError):self.exporter.reconcile(**request)
        with patch.object(Path,"is_symlink",lambda path:path==manifest or original_symlink(path)):
            with self.assertRaises(DocumentError):self.exporter.reconcile(**request)
        extra=destination/"example.agscene"/"unreferenced.bin"
        extra.write_bytes(b"extra")
        with patch.object(Path,"is_symlink",lambda path:path==extra or original_symlink(path)):
            with self.assertRaises(DocumentError):self.exporter.reconcile(**request)
        original_file=Path.is_file
        with patch.object(Path,"is_file",lambda path:False if path==extra else original_file(path)):
            with self.assertRaises(DocumentError):self.exporter.reconcile(**request)
        extra.unlink()
        extras=[destination/"example.agscene"/f"extra-{index}.bin" for index in range(50)]
        for item in extras:item.write_bytes(b"")
        with self.assertRaises(DocumentError):self.exporter.reconcile(**request)

    def test_export_root_and_generation_type_races_are_rejected(self):
        request=self.request()
        destination=self.export_root/self.exporter._request("export-one",1,request["expected_source_digest"],
                                                              request["expected_workset_digest"])
        destination.write_bytes(b"occupied")
        with self.assertRaises(DocumentError):self.exporter.reconcile(**request)
        destination.unlink()
        original_symlink=Path.is_symlink
        with patch.object(Path,"is_symlink",lambda path:path==self.export_root or original_symlink(path)):
            with self.assertRaises(DocumentError):self.exporter.reconcile(**request)
        forged=PackageSnapshot("bad",self.editor.snapshot.files,self.editor.snapshot.descriptor_paths)
        forged_editor=PackageEditor(forged,self.fixture.editor,max_bytes=65536,max_resource_bytes=1024)
        forged_store=DescriptorStore(self.fixture.root.parent/"forged.db",forged_editor,scope="source",actor="actor",
                                      authorize=lambda scope:True,max_events=8,max_total_bytes=1048576,create=True)
        with self.assertRaises(DocumentError):PackageExport(self.workspace,forged_store,actor="actor",source_key="package",
                                                            export_root=self.export_root,max_generations=1)

    def test_export_rechecks_source_and_saved_version_before_visibility(self):
        request=self.request()
        current=self.workspace.read()
        bad=PackageSnapshot(current.root_name,(("other",b"bytes"),),current.descriptor_paths)
        with patch.object(self.workspace,"read",return_value=bad):
            with self.assertRaises(DocumentError):self.exporter.export(**request)
        self.assertFalse(self.exporter.reconcile(**request)["recorded"])
        original=self.store.read; calls=0
        def read(**kwargs):
            nonlocal calls
            calls+=1
            result=original(**kwargs)
            if calls==2:result["digest"]="0"*64
            return result
        with patch.object(self.store,"read",side_effect=read):
            with self.assertRaises(DocumentError):self.exporter.export(**request)
        self.assertFalse(self.exporter.reconcile(**request)["recorded"])
        original_read=self.workspace.read
        def appeared():
            destination=self.export_root/self.exporter._request("export-one",1,request["expected_source_digest"],
                                                                  request["expected_workset_digest"])
            destination.mkdir()
            return original_read()
        with patch.object(self.workspace,"read",side_effect=appeared):
            with self.assertRaises(DocumentError):self.exporter.export(**request)

    def test_export_revocation_before_dispatch_or_visibility_preserves_reconciliation(self):
        request=self.request()
        allowed=[False]
        guarded=PackageExport(self.workspace,self.store,actor="actor",source_key="package",
            export_root=self.export_root,max_generations=2,authorize_write=lambda:allowed[0])
        with self.assertRaises(DocumentError):guarded.export(**request)
        self.assertFalse(guarded.reconcile(**request)["recorded"])
        allowed[0]=True
        checks=[0]
        def current():
            checks[0]+=1
            return checks[0]<3
        staged=PackageExport(self.workspace,self.store,actor="actor",source_key="package",
            export_root=self.export_root,max_generations=2,authorize_write=current)
        with self.assertRaises(DocumentError):staged.export(**request)
        self.assertEqual([],list(self.export_root.iterdir()))
        allowed[0]=True
        self.assertTrue(guarded.export(**request)["recorded"])
        allowed[0]=False
        with self.assertRaises(DocumentError):guarded.export(**request)
        self.assertTrue(guarded.reconcile(**request)["recorded"])

    def test_ai_export_tool_binds_host_paths_and_reconciles_exact_operation(self):
        tools=PackageExportTools(self.exporter,max_input_bytes=4096)
        names=[item["name"] for item in export_definitions()]
        self.assertEqual(["facetwire.package.export","facetwire.package.export_reconcile"],names)
        request=self.request()
        arguments=dict(session_id=tools.session_id,**request)
        self.assertFalse(tools.call(names[1],arguments)["recorded"])
        receipt=tools.call(names[0],arguments)
        self.assertEqual("immutable_package_generation",receipt["effect"])
        self.assertFalse(receipt["original_overwrite"])
        self.assertEqual(receipt["generation"],tools.call(names[1],arguments)["generation"])
        self.assertEqual(receipt["generation"],tools.call(names[0],arguments)["generation"])
        for name,args in (("facetwire.package.save",arguments),(names[0],dict(arguments,session_id="0"*32)),
                          (names[0],dict(arguments,export_root="other")),(names[0],dict(arguments,expected_source_revision=0)),
                          (names[0],dict(arguments,expected_workset_digest="bad"))):
            with self.assertRaises(DocumentError):tools.call(name,args)
        for name in (None,123,[]):
            with self.assertRaises(DocumentError):tools.call(name,arguments)
        with self.assertRaises(DocumentError):PackageExportTools(None,max_input_bytes=4096)
        with self.assertRaises(DocumentError):PackageExportTools(self.exporter,max_input_bytes=0)
        small=PackageExportTools(self.exporter,max_input_bytes=1)
        with self.assertRaises(DocumentError):small.call(names[1],dict(session_id=small.session_id,**request))

    def test_export_byte_and_single_file_limits_do_not_materialize(self):
        initial_total=sum(len(raw) for _,raw in self.workspace.read().files)
        limited=self.fixture.open(max_total_bytes=initial_total)
        exporter=PackageExport(limited,self.store,actor="actor",source_key="package",
                               export_root=self.export_root,max_generations=2)
        raw=self.editor.prepare_patch(self.initial,(dict(path="resources/note.txt",
            expected_digest=hashlib.sha256(b"synthetic note").hexdigest(),
            base64=base64.b64encode(b"x"*1024).decode("ascii"),scope="definition_all_instances"),))
        self.store.save(raw,actor="actor",source_key="package",expected_revision=1,
                        operation_id="large",mode="managed_local")
        with self.assertRaises(DocumentError):exporter.export(**self.request(raw,2,"total-too-large"))
        single=self.fixture.open(max_file_bytes=max(len(body) for _,body in self.workspace.read().files))
        other=PackageExport(single,self.store,actor="actor",source_key="package",
                            export_root=self.export_root,max_generations=2)
        with self.assertRaises(DocumentError):other.export(**self.request(raw,2,"file-too-large"))
        self.assertEqual([],list(self.export_root.iterdir()))
