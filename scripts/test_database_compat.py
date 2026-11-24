"""Run the unchanged upstream DB suite through a test-process-only adapter."""
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

def main():
    import backend.app
    spec=importlib.util.spec_from_file_location('fullstack_legacy_test_adapter',ROOT/'tests'/'legacy_api_adapter.py')
    adapter=importlib.util.module_from_spec(spec);spec.loader.exec_module(adapter)
    with patch.object(backend.app,'AppHandler',adapter.AppHandler,create=True):
        suite=unittest.defaultTestLoader.loadTestsFromName('db.test_database')
        result=unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1

if __name__=='__main__': raise SystemExit(main())
