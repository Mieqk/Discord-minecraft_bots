import os, sys, pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DISCORD_TOKEN", "test")


@pytest.fixture
def store(tmp_path):
    import store as s
    s.init(str(tmp_path))
    return s
