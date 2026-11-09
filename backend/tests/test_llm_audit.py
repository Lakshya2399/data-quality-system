from pydantic import BaseModel

from fakes import make_llm


class Out(BaseModel):
    answer: int


def test_with_audit_adds_a_hook_without_touching_the_original():
    base = make_llm(['{"answer": 1}', '{"answer": 2}'])
    seen_base, seen_scoped = [], []
    base.audit = lambda *a: seen_base.append(a[0])
    scoped = base.with_audit(lambda *a: seen_scoped.append(a[0]))

    assert scoped.generate_json("P", Out, agent="scoped").answer == 1
    assert seen_base == ["scoped"] and seen_scoped == ["scoped"]          # both hooks fire on the clone
    assert base.generate_json("P", Out, agent="plain").answer == 2
    assert seen_base == ["scoped", "plain"] and seen_scoped == ["scoped"]  # the original never reports to the clone's hook
