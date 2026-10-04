"""Strict evidence-bound answer contract and deterministic rendering.

Facts are literal excerpts. Inferences are labelled, require quoted support and
explicit assumptions; these structural checks are not a semantic accuracy guarantee.
"""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class Claim(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal['fact', 'inference', 'scenario', 'background']
    text: str = Field(min_length=1, max_length=1500)
    evidence_id: str | None = None
    quote: str = Field(default='', max_length=1500)
    assumptions: list[str] = Field(default_factory=list, max_length=5)


class GroundedAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claims: list[Claim] = Field(default_factory=list, max_length=10)
    next_checks: list[str] = Field(default_factory=list, max_length=5)
    insufficient_evidence: bool = False


def check_answer(data: dict, evidence: dict[str, dict], mode: str) -> GroundedAnswer:
    import re
    answer = GroundedAnswer.model_validate(data)
    if answer.insufficient_evidence:
        if answer.claims:
            raise ValueError('Insufficient evidence must not contain conclusions')
        return answer
    if not answer.claims:
        raise ValueError('Answer must contain claims or explicitly report insufficient evidence')
    for claim in answer.claims:
        if mode == 'background':
            if claim.kind != 'background' or claim.evidence_id or claim.quote:
                raise ValueError('Background explanations must not impersonate verified evidence')
            continue
        if mode == 'scenario':
            if claim.kind != 'scenario' or not claim.assumptions:
                raise ValueError('Scenarios must be conditional and explicitly labelled')
            continue
        source = evidence.get(claim.evidence_id)
        if claim.kind in ('scenario', 'background') or not source or not claim.quote.strip() or claim.quote not in source['text']:
            raise ValueError('Claim has no matching support excerpt')
        if claim.kind == 'fact' and claim.text != claim.quote:
            raise ValueError('Facts must quote the source; interpretative paraphrases are inferences')
        if claim.kind == 'inference':
            if not claim.assumptions:
                raise ValueError('Inference requires explicit assumptions')
            if any(number not in claim.quote for number in re.findall(r'\d+(?:\.\d+)?', claim.text)):
                raise ValueError('Unsubstantiated numerical inference')
    return answer


def render_answer(answer: GroundedAnswer) -> str:
    labels = {'fact': '来源原文', 'inference': '条件性推断（待验证）', 'scenario': '情景假设（非已发生事实）', 'background': '基础解释（非实时核验）'}
    lines = []
    if answer.insufficient_evidence:
        lines.append('【证据不足】现有材料无法支持对这个问题的结论。')
    for claim in answer.claims:
        lines.append(f"【{labels[claim.kind]}】{claim.text}")
        if claim.evidence_id:
            lines.append(f"证据：{claim.evidence_id}")
        if claim.assumptions:
            lines.append('成立条件：' + '；'.join(claim.assumptions))
        if claim.kind == 'inference':
            lines.append('支持片段：' + claim.quote)
    if answer.next_checks:
        lines.append('待核验的问题：\n' + '\n'.join(answer.next_checks))
    return '\n\n'.join(lines)
