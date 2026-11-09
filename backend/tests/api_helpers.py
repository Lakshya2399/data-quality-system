from fastapi.testclient import TestClient

from app.main import create_app
from fakes import make_llm


class ScriptedFactory:
    """llm_factory for create_app: each API call that needs an LLM gets the next scripted session."""

    def __init__(self, *scripts):
        self.scripts = list(scripts)

    def __call__(self):
        if not self.scripts:
            raise AssertionError("the API asked for more LLM sessions than the test scripted")
        return make_llm(self.scripts.pop(0))


def make_client(env, data_dir, *scripts, factory=None, **kwargs) -> TestClient:
    app = create_app(llm_factory=factory or ScriptedFactory(*scripts), db_path=env.db, workdir=env.work,
                     data_dir=data_dir, **kwargs)
    return TestClient(app)
