# SPDX-License-Identifier: Apache-2.0
"""Synthetic, installed data profile; no Renderer code or real document."""
import base64
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from facetwire_tools.descriptor import DocumentError, encode
from facetwire_tools.extension_content import ExtensionContent, _has_ref
from facetwire_tools.package_editor import PackageEditor
from facetwire_tools.package_workspace import PackageSnapshot, PackageWorkspace
from fwtools_tests.test_package_workspace import PackageWorkspaceTests


def schema():
    return encode(dict(type='object',required=['type','body','ink'],properties={
        'type':dict(const='fact-card'),'body':dict(type='string',maxLength=80),
        'ink':dict(type='string',pattern=r'^#[0-9a-fA-F]{8}$')},additionalProperties=False))


class ExtensionContentTests(unittest.TestCase):
    def test_exact_external_profile_and_invalid_shapes(self):
        profiles=ExtensionContent((('fact-card',schema()),))
        profiles.validate(dict(type='fact-card',body='hello',ink='#112233ff'))
        for content in (None,{},dict(type=1),dict(type='missing'),
                        dict(type='fact-card',body='hi',ink='invalid'),
                        dict(type='fact-card',body='hi',ink='#112233ff',extra=True)):
            with self.subTest(content=content),self.assertRaises(DocumentError):
                profiles.validate(content)
        self.assertFalse(_has_ref(['plain',{'value':1}]))
        self.assertTrue(_has_ref([{'nested':{'$ref':'https://remote'}}]))
        for specs in ([],[('fact-card',schema())],tuple([('fact-card',schema())]*17),
                      (('fact-card',schema()),('fact-card',schema())),('bad',),
                      (('image',schema()),),(('Bad',schema()),),(('fact-card',b''),),
                      (('fact-card',b'not-json'),),
                      (('fact-card',encode(dict(type='object',properties=dict(type=dict(const='fact-card'))))),),
                      (('fact-card',encode(dict(type='object',required=['type'],additionalProperties=False,
                        properties=dict(type=dict(const='fact-card'),body=dict(**{'$ref':'https://remote'}))))),)):
            with self.subTest(specs=repr(specs)[:90]),self.assertRaises(DocumentError):ExtensionContent(specs)

    def test_pinned_file_and_changed_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            file=Path(directory).resolve()/'content.schema.json';file.write_bytes(schema())
            pin=dict(type='fact-card',schema_path=str(file),sha256=hashlib.sha256(schema()).hexdigest())
            self.assertEqual(ExtensionContent((('fact-card',schema()),)).digest,ExtensionContent.load([pin]).digest)
            for entries in ([],[dict(pin,extra=True)],[dict(pin,sha256='bad')],
                            [dict(pin,schema_path='relative.json')],
                            [dict(pin,schema_path=str(file/'not-file'))]):
                with self.subTest(entries=entries),self.assertRaises(DocumentError):ExtensionContent.load(entries)
            file.write_bytes(b'changed')
            with self.assertRaises(DocumentError):ExtensionContent.load([pin])

    def test_package_inventory_and_edit_are_bound_to_installed_schema(self):
        fixture=PackageWorkspaceTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        custom=dict(type='fact-card',body='synthetic',ink='#112233ff')
        fixture.child['canvas']['pages'][0]['layers'][0]['zones'][-1]['content']=custom
        fixture.save()
        with self.assertRaises(DocumentError):fixture.open().read()
        profiles=ExtensionContent((('fact-card',schema()),))
        workspace=fixture.open(extensions=profiles)
        snapshot=workspace.read()
        self.assertEqual(profiles.digest,snapshot.extension_digest)
        with self.assertRaises(DocumentError):PackageWorkspace(fixture.root,fixture.editor,
            max_files=8,max_total_bytes=65536,max_depth=3,max_file_bytes=65536,extensions=object())
        with self.assertRaises(DocumentError):PackageEditor(snapshot,fixture.editor,max_bytes=65536,max_resource_bytes=1024)
        unbound=PackageSnapshot(snapshot.root_name,snapshot.files,snapshot.descriptor_paths)
        with self.assertRaises(DocumentError):PackageEditor(unbound,fixture.editor,max_bytes=65536,max_resource_bytes=1024)
        editor=PackageEditor(snapshot,fixture.editor,max_bytes=65536,max_resource_bytes=1024,extensions=profiles)
        self.assertEqual('facetwire.package-draft.v1',editor.inspect(editor.initial())['profile'])
        child_path='documents/child.agscene/child.agscene.dis.json'
        patch=dict(descriptor_path=child_path,expected_descriptor_digest=hashlib.sha256(dict(snapshot.files)[child_path]).hexdigest(),
            target_id='picture',field='extension.ink',expected='#112233ff',value='#aabbccff',scope='definition_all_instances')
        edited=editor.prepare_patch(editor.initial(),(patch,))
        changed_child=next(item for item in json.loads(edited)['files'] if item['path']==child_path)
        self.assertEqual('#aabbccff',json.loads(base64.b64decode(changed_child['base64']))
                         ['canvas']['pages'][0]['layers'][0]['zones'][-1]['content']['ink'])
        self.assertEqual('#112233ff',fixture.child['canvas']['pages'][0]['layers'][0]['zones'][-1]['content']['ink'])
        with self.assertRaises(DocumentError):editor.prepare_patch(edited,(patch,))
        for field,expected,value in (('extension.type','fact-card','other'),('extension.missing','x','y'),
                                     ('extension.','x','y'),('extension.ink','wrong','#aabbccff'),
                                     ('extension.ink','#112233ff','bad')):
            with self.subTest(field=field,value=value),self.assertRaises(DocumentError):
                editor.prepare_patch(editor.initial(),(dict(patch,field=field,expected=expected,value=value),))
        with self.assertRaises(DocumentError):editor.prepare_patch(editor.initial(),(dict(patch,target_id='root'),))
        with self.assertRaises(DocumentError):editor.prepare_patch(editor.initial(),(dict(patch,field='extension.body',
            expected='synthetic',value=42),))
        new=editor.prepare_patch(editor.initial(),(dict(descriptor_path='example.agscene.dis.json',
            expected_descriptor_digest=hashlib.sha256(dict(snapshot.files)['example.agscene.dis.json']).hexdigest(),
            target_id='root',field='title',expected='Synthetic CLI',value='New synthetic',scope='definition_all_instances'),))
        root=next(item for item in json.loads(new)['files'] if item['path']=='example.agscene.dis.json')
        self.assertEqual('New synthetic',json.loads(base64.b64decode(root['base64']))['title'])
        fixture.child['canvas']['pages'][0]['layers'][0]['zones'][-1]['content']['ink']='bad'
        fixture.save()
        with self.assertRaises(DocumentError):workspace.read()
