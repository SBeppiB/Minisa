import importlib.util
from pathlib import Path


def test_quality_fixture(engine):
    spec = importlib.util.spec_from_file_location(
        "quality", Path(__file__).parents[1] / "benchmarks" / "quality_benchmark.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    report = module.evaluate(engine)
    assert report["queries"] == 12
    assert report["corpus_size"] == 24
    assert 0 <= report["recall_at_1"] <= report["recall_at_5"] <= 1
    assert report["rows"][0]["rank"] is not None
