import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from standalone_audit import Audit, safe_member


class PackagingSafetyTests(unittest.TestCase):
    def test_rejects_private_identifiers_in_plain_and_utf16(self):
        for raw in (b'C:\\Users\\example', 'C:\\Users\\example'.encode('utf-16-le')):
            with self.assertRaisesRegex(RuntimeError, 'Private-identifier'):
                Audit().data('example', raw)

    def test_rejects_private_bytecode_filename(self):
        with self.assertRaisesRegex(RuntimeError, 'Private-identifier'):
            Audit().code('test', compile('value = 1', 'C:/Users/example/source.py', 'exec'))

    def test_rejects_private_bytecode_constant(self):
        with self.assertRaisesRegex(RuntimeError, 'Private-identifier'):
            Audit().code('test', compile('value = "C:/Users/example"', 'source.py', 'exec'))

    def test_rejects_server_data_and_traversal(self):
        for name in ('../private', '/absolute', 'C:/absolute', 'a\\b',
                     'worlds/map/file', 'BACKUPS/data.zip', 'server.properties', 'bedrock_server.exe'):
            with self.subTest(name=name), self.assertRaises(RuntimeError):
                safe_member(name)

    def test_checks_nested_zip_contents(self):
        inner = io.BytesIO()
        with zipfile.ZipFile(inner, 'w') as archive:
            archive.writestr('file.txt', 'C:/Users/example')
        outer = io.BytesIO()
        with zipfile.ZipFile(outer, 'w') as archive:
            archive.writestr('nested.zip', inner.getvalue())
        with self.assertRaisesRegex(RuntimeError, 'Private-identifier'):
            Audit().data('outer.zip', outer.getvalue())

    def test_exact_allowlist_rejects_extra_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'unexpected.txt').write_text('sample')
            with self.assertRaisesRegex(RuntimeError, 'allowlist mismatch'):
                Audit().folder(root, set())

    def test_exact_allowlist_rejects_missing_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(RuntimeError, 'allowlist mismatch'):
                Audit().folder(Path(tmp), {'missing.dll'})

    def test_clean_payload_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'example.txt').write_text('Clean default')
            result = Audit().folder(root, {'example.txt'})
            self.assertEqual(len(result['example.txt']), 64)

    def test_rejects_onefile_extraction_records(self):
        for kind in ('b', 'x', 'Z', 'n'):
            archive = SimpleNamespace(options=['pyi-contents-directory _internal'],
                                      toc={'payload': (0, 0, 0, 0, kind)})
            with self.subTest(kind=kind), patch('standalone_audit.CArchiveReader', return_value=archive):
                with self.assertRaisesRegex(RuntimeError, 'self-extracting'):
                    Audit().executable(Path('example.exe'))

    def test_rejects_runtime_folder_mismatch(self):
        archive = SimpleNamespace(options=[], toc={})
        with patch('standalone_audit.CArchiveReader', return_value=archive):
            with self.assertRaisesRegex(RuntimeError, 'external _internal'):
                Audit().executable(Path('example.exe'))
