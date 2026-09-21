"""Separate label-blind candidate/automatic decisions from held-out scoring."""
from datetime import datetime, timedelta, timezone
from trace.db.connection import Database
from trace.db.migration import apply_migrations
from trace.db.repositories import EventRepo, RawItemRepo
from trace.domain.models import Event, RawItem
from trace.event_engine.exact_dedup import ExactDedup
from trace.event_engine.normalize import normalize_raw_item
from trace.event_engine.semantic_cluster import SemanticCluster
from trace.event_engine.embeddings import HashEmbedder


def predict_pair(item_a: dict, item_b: dict, config) -> dict:
    # Arguments are only raw documents, never a pair containing expected labels.
    db = Database(':memory:')
    apply_migrations(db)
    db.conn.execute('PRAGMA foreign_keys=OFF')
    now = datetime.now(timezone.utc)
    try:
        def raw(data, id, when):
            return normalize_raw_item(RawItem(raw_item_id=id, source_id=data['source_id'],
                source_item_id=data.get('source_item_id'), title=data['title'],content=data.get('content'),
                url=data.get('canonical_url',''),canonical_url=data.get('canonical_url',''),
                language=data.get('language',''),published_at=when,fetched_at=when))
        a, b = raw(item_a,'raw-a',now), raw(item_b,'raw-b',now+timedelta(minutes=10))
        RawItemRepo(db).insert(a)
        event = Event(event_id='event-a',title=a.title,summary=(a.content or '')[:200],
                      event_type=item_a.get('event_type','other'),first_seen_at=now,last_updated_at=now,event_time=now)
        EventRepo(db).insert(event)
        RawItemRepo(db).link_event(a.raw_item_id,event.event_id)
        duplicate = ExactDedup(db).check(b)
        if duplicate.is_duplicate or duplicate.is_revision:
            return {'decision':'merge','candidate_retrieved':True,'reason':duplicate.reason}
        cluster = SemanticCluster(db,HashEmbedder(256),config)
        candidates = cluster.find_candidates(title=b.title,summary=(b.content or '')[:200],entities=item_b.get('entities',[]),
            event_type=item_b.get('event_type','other'),event_time=b.published_at,language=b.language)
        if not candidates:
            return {'decision':'separate','candidate_retrieved':False}
        score = candidates[0].merge_score
        return {'decision':'merge' if score>=cluster.auto_merge_threshold else 'needs_verifier',
                'candidate_retrieved': score>=cluster.verifier_min_threshold, 'score':round(score,6)}
    finally:
        db.close()


def evaluate_pairs(predictions: list[dict], labels: list[bool], *, verifier_fn=None, pairs=None) -> dict:
    if len(predictions) != len(labels):
        raise ValueError('Mismatched prediction and label counts')
    positives = sum(labels)
    verifier_evaluated = False
    verifier_correct = 0
    verifier_false = 0
    if verifier_fn and pairs and len(pairs) == len(predictions):
        verifier_evaluated = True
        for p, y, pair in zip(predictions, labels, pairs):
            if p['decision'] == 'needs_verifier':
                decision = verifier_fn(pair['item_a'], pair['item_b'])
                if decision:
                    if y:
                        verifier_correct += 1
                    else:
                        verifier_false += 1
    return {
        'samples': len(labels),
        'candidate_recall': sum(p['candidate_retrieved'] and y for p, y in zip(predictions, labels)) / positives if positives else None,
        'automatic_false_merges': sum(p['decision'] == 'merge' and not y for p, y in zip(predictions, labels)),
        'automatic_correct_merges': sum(p['decision'] == 'merge' and y for p, y in zip(predictions, labels)),
        'needs_verifier': sum(p['decision'] == 'needs_verifier' for p in predictions),
        'verifier_evaluated': verifier_evaluated,
        'verifier_correct_merges': verifier_correct if verifier_evaluated else None,
        'verifier_false_merges': verifier_false if verifier_evaluated else None,
        'verifier_status': 'evaluated' if verifier_evaluated else 'pending_real_verifier',
        'provenance': 'legacy regression fixtures; independent human annotation not verified',
    }
