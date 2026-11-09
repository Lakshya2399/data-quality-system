"""A scripted stand-in for the google-genai client. It sits UNDER the real GeminiLLM, so JSON
extraction, validation, validator feedback and retry logic are all the production code paths."""
from types import SimpleNamespace

from app.llm import GeminiLLM


class TransientError(Exception):
    """Looks like google.genai.errors.APIError: has an HTTP `code`."""

    def __init__(self, code: int = 503):
        super().__init__(f"HTTP {code}")
        self.code = code


class FakeModels:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def generate_content(self, model, contents, config):
        self.calls.append(SimpleNamespace(model=model, prompt=contents, config=config))
        if not self.script:
            raise AssertionError("fake LLM script exhausted: the code made more LLM calls than expected")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return SimpleNamespace(text=item)


class FakeClient:
    def __init__(self, script):
        self.models = FakeModels(script)


def make_llm(script, **kwargs) -> GeminiLLM:
    """GeminiLLM over a scripted client. llm.calls records every request; llm.sleeps every backoff."""
    sleeps: list[float] = []
    llm = GeminiLLM(client=FakeClient(script), sleep=sleeps.append, **kwargs)
    llm.calls = llm._client.models.calls
    llm.sleeps = sleeps
    return llm
