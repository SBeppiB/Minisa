import pytest

from universal_memory import MemoryEngine, Settings


@pytest.fixture
def engine(tmp_path):
    with MemoryEngine(tmp_path / "memory", settings=Settings(backup_interval_seconds=0)) as memory:
        yield memory
