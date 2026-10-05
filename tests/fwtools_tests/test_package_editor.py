# SPDX-License-Identifier: Apache-2.0
"""Synthetic package resource drafts using the existing durable transaction engine."""
import base64
import hashlib
import json
import unittest

from facetwire_tools.descriptor import DocumentError, encode
from facetwire_tools.journal import DraftJournal
from facetwire_tools.managed_save import ManagedSaves
from facetwire_tools.package_editor import PackageEditor, _drop
from facetwire_tools.storage import DescriptorStore
from facetwire_tools.tool_api import DocumentTools, definitions as draft_definitions
from facetwire_tools.storage_tools import StorageTools, definitions as source_definitions
from facetwire_tools.managed_tools import ManagedSaveTools, definitions as save_definitions
from fwtools_tests.test_package_workspace import PackageWorkspaceTests


class PackageEditorTests(unittest.TestCase):
    def setUp(self):
        fixture = PackageWorkspaceTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.snapshot = fixture.open().read()
        self.editor = PackageEditor(self.snapshot, fixture.editor, max_bytes=65536, max_resource_bytes=1024)
        self.initial = self.editor.initial()
        self.resource = "resources/note.txt"

    def operation(self, before=b"synthetic note", after=b"replacement"):
        return dict(path=self.resource, expected_digest=hashlib.sha256(before).hexdigest(),
                    base64=base64.b64encode(after).decode("ascii"), scope="definition_all_instances")

    def descriptor_operation(self, path="documents/child.agscene/child.agscene.dis.json", target_id="picture",
                             field="bounds", expected=None, value=None):
        descriptor=dict(self.snapshot.files)[path]
        return dict(descriptor_path=path,expected_descriptor_digest=hashlib.sha256(descriptor).hexdigest(),
                    target_id=target_id,field=field,
                    expected=expected if expected is not None else {"x":0,"y":120,"width":100,"height":100},
                    value=value if value is not None else {"x":10,"y":120,"width":100,"height":100},
                    scope="definition_all_instances")

    def test_private_preview_requires_exact_scope_cas_and_validated_bytes(self):
        changed = self.editor.prepare_patch(self.initial, (self.operation(),))
        self.assertEqual("facetwire.package-draft.v1", self.editor.inspect(changed)["profile"])
        self.assertNotEqual(self.editor.inspect(self.initial)["workset_digest"], self.editor.inspect(changed)["workset_digest"])
        self.assertEqual(b"synthetic note", (self.fixture.root/self.resource).read_bytes())
        with self.assertRaises(DocumentError):self.editor.prepare_patch(changed, (self.operation(),))
        for changed_op in (dict(self.operation(), scope="one_instance"),
                           dict(self.operation(), path="example.agscene.dis.json"),
                           dict(self.operation(), expected_digest="0"*64),
                           dict(self.operation(), base64="?"),
                           dict(self.operation(), base64="YR=="),
                           dict(self.operation(), base64=23),
                           dict(self.operation(), base64=base64.b64encode(b"x"*1025).decode("ascii")),
                           dict(self.operation(), extra=True), None, object()):
            with self.assertRaises(DocumentError):self.editor.prepare_patch(self.initial, (changed_op,))
        for operations in ([], [self.operation()], tuple(), tuple(self.operation() for _ in range(129))):
            with self.assertRaises(DocumentError):self.editor.prepare_patch(self.initial, operations)

    def test_canonical_source_binding_and_invalid_inputs_fail_closed(self):
        style={"style":{"color":"red"}}
        _drop(style,("style","color"))
        self.assertEqual({},style)
        with self.assertRaises(DocumentError):self.editor._descriptor(b"x"*65537)
        for value in (None, object(), b"", b"{", self.initial+b" ", b"x"*65537):
            with self.assertRaises(DocumentError):self.editor.parse(value)
        source=json.loads(self.initial)
        for changed in (dict(source,id="other"), dict(source,profile="other"), dict(source,files=[]),
                        dict(source,extra=True)):
            with self.assertRaises(DocumentError):self.editor.parse(encode(changed))
        for path, body in (("other", source["files"][0]["base64"]),
                           (self.resource,"bad!"), (self.resource,base64.b64encode(b"x"*1025).decode("ascii"))):
            data=json.loads(self.initial)
            index=next(i for i,item in enumerate(data["files"]) if item["path"]==self.resource)
            data["files"][index]=dict(path=path,base64=body)
            with self.assertRaises(DocumentError):self.editor.parse(encode(data))
        data=json.loads(self.initial)
        index=next(i for i,item in enumerate(data["files"]) if item["path"] in self.snapshot.descriptor_paths)
        data["files"][index]["base64"]=base64.b64encode(b"{}").decode("ascii")
        with self.assertRaises(DocumentError):self.editor.parse(encode(data))
        with self.assertRaises(DocumentError):PackageEditor(None,self.fixture.editor,max_bytes=65536,max_resource_bytes=1024)
        with self.assertRaises(DocumentError):PackageEditor(self.snapshot,None,max_bytes=65536,max_resource_bytes=1024)
        with self.assertRaises(DocumentError):PackageEditor(self.snapshot,self.fixture.editor,max_bytes=0,max_resource_bytes=1024)

    def test_nested_descriptor_patch_binds_definition_and_all_instances(self):
        self.fixture.parent["canvas"]["pages"][0]["layers"][0]["zones"].append({
            "id":"second-nested","bounds":{"x":310,"y":120,"width":300,"height":200},
            "content":{"type":"document","source":"documents/child.agscene/child.agscene.dis.json"}})
        self.fixture.save()
        snapshot=self.fixture.open().read()
        editor=PackageEditor(snapshot,self.fixture.editor,max_bytes=65536,max_resource_bytes=1024)
        self.assertEqual("ambiguous",self.fixture.open().targets("picture",max_matches=2)["status"])
        located=editor.targets(editor.initial(),"picture",max_matches=2)
        self.assertEqual("ambiguous",located["status"])
        self.assertEqual(2,len(located["targets"]))
        self.assertNotEqual(located["targets"][0]["instance_key"],located["targets"][1]["instance_key"])
        self.assertEqual("not_found",editor.targets(editor.initial(),"missing",max_matches=1)["status"])
        with self.assertRaises(DocumentError):editor.targets(editor.initial(),"picture",max_matches=1)
        for target,limit in ((None,1),("",1),("x"*513,1),("picture",0)):
            with self.assertRaises(DocumentError):editor.targets(editor.initial(),target,max_matches=limit)
        operation=self.descriptor_operation()
        changed=editor.prepare_patch(editor.initial(),(operation,))
        self.assertNotEqual(located["workset_digest"],editor.targets(changed,"picture",max_matches=2)["workset_digest"])
        path=operation["descriptor_path"]
        raw=base64.b64decode(next(item["base64"] for item in json.loads(changed)["files"] if item["path"]==path))
        self.assertEqual(10,json.loads(raw)["canvas"]["pages"][0]["layers"][0]["zones"][-1]["bounds"]["x"])
        self.assertEqual(dict(snapshot.files)[path],(self.fixture.root/path).read_bytes())
        with self.assertRaises(DocumentError):editor.prepare_patch(changed,(operation,))
        for bad in (dict(operation,scope="one_instance"),dict(operation,descriptor_path=self.resource),
                    dict(operation,expected_descriptor_digest="0"*64),dict(operation,target_id="missing"),
                    dict(operation,field="resource"),dict(operation,expected={}),
                    dict(operation,value={"x":0,"y":120,"width":"invalid","height":100}),
                    dict(operation,extra=True)):
            with self.assertRaises(DocumentError):editor.prepare_patch(editor.initial(),(bad,))
        value=json.loads(editor.initial())
        target=next(item for item in value["files"] if item["path"]==path)
        mutated=json.loads(dict(snapshot.files)[path]);mutated["resources"][0]["source"]="resources/other.bin"
        target["base64"]=base64.b64encode(encode(mutated)).decode("ascii")
        with self.assertRaises(DocumentError):editor.parse(encode(value))
        mutated=json.loads(dict(snapshot.files)[path]);mutated["canvas"]["pages"][0]["layers"][0]["zones"][-1]["id"]="zone"
        target["base64"]=base64.b64encode(encode(mutated)).decode("ascii")
        with self.assertRaises(DocumentError):editor.parse(encode(value))

    def test_existing_journal_managed_save_undo_redo_and_dirty_reload(self):
        path=self.fixture.root.parent/"package-history.db"
        authorize=lambda scope: True
        journal=DraftJournal(path,self.editor,scope="package",actor="actor",authorize=authorize,
                             max_events=16,max_total_bytes=1048576,create=True,initial=self.initial)
        store=DescriptorStore(path,self.editor,scope="package-source",actor="actor",authorize=authorize,
                              max_events=16,max_total_bytes=1048576,create=True)
        store.save(self.initial,actor="actor",source_key="source",expected_revision=0,
                   operation_id="initial",mode="managed_local")
        saves=ManagedSaves(journal,store,actor="actor",source_key="source",max_saves=8,max_total_bytes=65536,create=True)
        edit=dict(operation_id="edit",actor="actor",action="patch",revision=1,head=1,payload=[self.operation()])
        committed=journal.apply(edit)
        self.assertTrue(committed["dirty"])
        self.assertEqual(committed,journal.reconcile(actor="actor",operation_id="edit")["receipt"]|{"operation_id":"edit","duplicate":False})
        changed=journal.snapshot(actor="actor",expected_revision=2,max_bytes=65536)["descriptor"]
        self.assertEqual("replacement",base64.b64decode(next(item["base64"] for item in json.loads(changed)["files"]
            if item["path"]==self.resource)).decode("utf-8"))
        saved=saves.save(dict(operation_id="save",expected_revision=2,expected_head=2,expected_source_revision=1))
        self.assertEqual(changed,store.read(actor="actor",source_key="source",expected_revision=2,max_bytes=65536)["descriptor"])
        self.assertTrue(saves.reconcile("save")["recorded"])
        self.assertFalse(saved["receipt"]["draft"]["dirty"])
        journal.apply(dict(operation_id="undo",actor="actor",action="undo",revision=3,head=2,payload=1))
        self.assertTrue(journal.snapshot(actor="actor",expected_revision=4,max_bytes=65536)["dirty"])
        journal.apply(dict(operation_id="redo",actor="actor",action="redo",revision=4,head=1,payload=1))
        self.assertEqual(changed,journal.snapshot(actor="actor",expected_revision=5,max_bytes=65536)["descriptor"])
        journal.apply(dict(operation_id="undo-again",actor="actor",action="undo",revision=5,head=2,payload=1))
        command=dict(operation_id="reload",actor="actor",action="reload",revision=6,head=1,
                     payload=dict(descriptor_utf8=changed.decode("utf-8"),dirty_policy="reject",approval_id=None,
                                  source=dict(scope="package-source",source_key="source",revision=2,
                                              digest=hashlib.sha256(changed).hexdigest())))
        with self.assertRaises(DocumentError):journal.reload(command,verify_source=lambda raw,scope:True,
                                                                approve_discard=lambda raw,scope:False)
        command["payload"]["dirty_policy"]="discard";command["payload"]["approval_id"]="explicit"
        journal.reload(command,verify_source=lambda raw,scope:True,approve_discard=lambda raw,scope:True)
        self.assertFalse(journal.snapshot(actor="actor",expected_revision=7,max_bytes=65536)["dirty"])
        journal=DraftJournal(path,self.editor,scope="package",actor="actor",authorize=authorize,
                             max_events=16,max_total_bytes=1048576,create=False,initial=None)
        self.assertEqual(changed,journal.snapshot(actor="actor",expected_revision=7,max_bytes=65536)["descriptor"])

    def test_package_ai_tools_use_distinct_schemas_and_shared_durable_source(self):
        path=self.fixture.root.parent/"package-tools.db"
        authorize=lambda scope: True
        journal=DraftJournal(path,self.editor,scope="package",actor="actor",authorize=authorize,
                             max_events=16,max_total_bytes=1048576,create=True,initial=self.initial)
        store=DescriptorStore(path,self.editor,scope="package-source",actor="actor",authorize=authorize,
                              max_events=16,max_total_bytes=1048576,create=True)
        store.save(self.initial,actor="actor",source_key="source",expected_revision=0,
                   operation_id="initial",mode="managed_local")
        saves=ManagedSaves(journal,store,actor="actor",source_key="source",max_saves=8,
                           max_total_bytes=65536,create=True)
        draft=DocumentTools(journal,actor="actor",max_input_bytes=65536,max_output_bytes=131072)
        source=StorageTools(journal,store,actor="actor",source_key="source",approve_discard=lambda raw,scope:False,
                            max_input_bytes=65536,max_output_bytes=131072)
        managed=ManagedSaveTools(saves,max_input_bytes=65536)
        self.assertEqual(10,len(draft_definitions(package=True)))
        self.assertEqual(4,len(source_definitions(package=True)))
        self.assertEqual(2,len(save_definitions(package=True)))
        self.assertEqual("facetwire.package.apply_patch",draft_definitions(package=True)[3]["name"])
        self.assertEqual("facetwire.package.save",save_definitions(package=True)[0]["name"])
        args=lambda handle,**values:dict(session_id=handle.session_id,**values)
        with self.assertRaises(DocumentError):draft.call("facetwire.document.inspect",args(draft,expected_revision=1))
        with self.assertRaises(DocumentError):draft.call("facetwire.package.apply_patch",
            args(draft,operation_id="invalid",expected_revision=1,expected_head=1,operations=[dict(self.operation(),scope="one_instance")]))
        self.assertTrue(draft.call("facetwire.package.preview_patch",args(draft,expected_revision=1,
            expected_head=1,operations=[self.descriptor_operation()]))["candidate"]["workset_digest"])
        located=draft.call("facetwire.package.targets",args(draft,expected_revision=1,
            target_id="picture",max_matches=2))
        self.assertEqual("unique",located["status"])
        self.assertEqual("documents/child.agscene/child.agscene.dis.json",located["targets"][0]["descriptor_path"])
        preview=draft.call("facetwire.package.preview_patch",
            args(draft,expected_revision=1,expected_head=1,operations=[self.operation()]))
        self.assertFalse(preview["persisted"])
        self.assertEqual(self.editor.inspect(self.initial)["workset_digest"],
                         draft.call("facetwire.package.validate",args(draft,expected_revision=1))["workset_digest"])
        applied=draft.call("facetwire.package.apply_patch",
            args(draft,operation_id="edit",expected_revision=1,expected_head=1,operations=[self.operation()]))
        self.assertEqual("durable_draft_only",applied["effect"])
        self.assertEqual(2,draft.call("facetwire.package.snapshot",args(draft,expected_revision=2))["memory_revision"])
        self.assertTrue(draft.call("facetwire.package.history",args(draft,after_revision=0,limit=8))["entries"])
        self.assertTrue(draft.call("facetwire.package.reconcile",args(draft,operation_id="edit"))["recorded"])
        saved=managed.call("facetwire.package.save",args(managed,operation_id="save",
            expected_revision=2,expected_head=2,expected_source_revision=1))
        self.assertTrue(saved["source_save"])
        self.assertTrue(managed.call("facetwire.package.save_reconcile",args(managed,operation_id="save"))["recorded"])
        self.assertEqual(2,source.call("facetwire.package_source.inspect",
            args(source,expected_source_revision=2))["revision"])
        self.assertIn("descriptor_utf8",source.call("facetwire.package_source.snapshot",
            args(source,expected_source_revision=2)))
        self.assertIn("descriptor_utf8",source.call("facetwire.package_source.reconcile",
            args(source,operation_id="managed-source-"+hashlib.sha256(encode("save")).hexdigest())))
        draft.call("facetwire.package.undo",args(draft,operation_id="undo",expected_revision=3,expected_head=2,count=1))
        draft.call("facetwire.package.redo",args(draft,operation_id="redo",expected_revision=4,expected_head=1,count=1))
        reloaded=source.call("facetwire.package.reload",args(source,expected_source_revision=2,
            expected_revision=5,expected_head=2,operation_id="reload",dirty_policy="reject",approval_id=None))
        self.assertEqual("durable_draft_reload",reloaded["effect"])
