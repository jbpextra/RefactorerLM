"""Verify the similarity scorer's threshold without loading a model."""

import sys
from types import SimpleNamespace

import pytest

from scripts import correctness_filter


def install_fake_codebert(monkeypatch, f1):
    calls = []
    tensor_reads = []

    def tensor(name, value):
        def item():
            tensor_reads.append(name)
            return value

        return SimpleNamespace(item=item)

    def score(**kwargs):
        calls.append(kwargs)
        return (
            tensor("precision", 0.11),
            tensor("recall", 0.22),
            tensor("f1", f1),
            tensor("f3", 0.99),
        )

    monkeypatch.setitem(sys.modules, "code_bert_score", SimpleNamespace(score=score))
    return calls, tensor_reads


@pytest.mark.parametrize("f1, passed", [(0.749, False), (0.75, True), (0.751, True)])
def test_check_similarity_uses_only_f1_with_inclusive_threshold(monkeypatch, f1, passed):
    calls, tensor_reads = install_fake_codebert(monkeypatch, f1)

    assert correctness_filter.check_similarity("after code", "before code", 0.75) == (
        passed,
        f1,
    )
    assert calls == [{"cands": ["after code"], "refs": ["before code"], "lang": "python"}]
    assert tensor_reads == ["f1"]
