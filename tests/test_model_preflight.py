import os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from one_trading.model_preflight import ensure_pinned_path

class ExecutableBootstrapTests(unittest.TestCase):
    def test_child_missing_path_repaired_locally_idempotently(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);binary=root/'bin'/'codex.exe';binary.parent.mkdir();binary.write_bytes(b'synthetic-not-executed')
            (root/'model-executable.txt').write_text(str(binary),encoding='utf-8')
            def which(_):return str(binary) if str(binary.parent) in os.environ['PATH'].split(os.pathsep) else None
            with patch.dict(os.environ,{'PATH':'old-path'}),patch('one_trading.model_preflight.shutil.which',side_effect=which):
                self.assertTrue(ensure_pinned_path(root));before=os.environ['PATH'];self.assertTrue(ensure_pinned_path(root));self.assertEqual(os.environ['PATH'],before)
    def test_missing_pin_does_not_substitute_executable_or_env(self):
        with tempfile.TemporaryDirectory() as temp,patch.dict(os.environ,{'PATH':'original'}):
            self.assertFalse(ensure_pinned_path(temp));self.assertEqual(os.environ['PATH'],'original')
    def test_unavailable_pinned_file_is_blocked(self):
        with tempfile.TemporaryDirectory() as temp,patch.dict(os.environ,{'PATH':'original'}):
            root=Path(temp);(root/'model-executable.txt').write_text(str(root/'missing.exe'),encoding='utf-8')
            self.assertFalse(ensure_pinned_path(root));self.assertEqual(os.environ['PATH'],'original')

if __name__=='__main__':unittest.main()
