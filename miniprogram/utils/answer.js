const KIND_LABELS = { fact: '原文引用', inference: '条件推断', scenario: '情景假设', background: '背景解释' };
function presentAnswer(response) {
  const claims = (Array.isArray(response.claims) ? response.claims : [])
    .filter(c => c && typeof c.text === 'string' && c.text.trim())
    .map((c, index) => ({
      id: c.claim_id || 'claim-' + index,
      text: c.text.trim(), kind: c.kind,
      label: KIND_LABELS[c.kind] || '回答',
      conditions: Array.isArray(c.assumptions) ? c.assumptions.filter(a => typeof a === 'string' && a.trim()) : [],
      supportQuote: typeof c.quote === 'string' ? c.quote : '',
      counterEvidence: Array.isArray(c.counter_evidence) ? c.counter_evidence.filter(a => typeof a === 'string' && a.trim()) : []
    }));
  const sources = [...new Set(Array.isArray(response.citations) ? response.citations : [])]
    .filter(url => typeof url === 'string' && /^https?:\/\//i.test(url))
    .map((url, index) => {
      const host = (url.match(/^https?:\/\/([^/]+)/i) || [])[1] || '原文链接';
      return { url, label: host.replace(/^www\./, ''), number: index + 1 };
    });
  const rawContent = response.answer || response.text || response.content || '';
  const content = typeof rawContent === 'string' ? rawContent : '';
  const match = content.match(/待核验的问题：[\s\S]*?(?=\n\n公司资料来源：|$)/);
  return {
    answerClaims: claims, answerSources: sources,
    nextChecksText: match ? match[0].trim() : '',
    detailsExpanded: false,
    hasAnswerDetails: (claims.length > 0 && (claims.some(c => c.conditions.length || c.supportQuote || c.counterEvidence.length) || !!match)) || sources.length > 0
  };
}
module.exports = { presentAnswer };
