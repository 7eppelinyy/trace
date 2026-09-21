"""Operational capability policy; configuration is not evidence of legal approval."""
def permitted(db, source_id, action):
    if action not in ('fetch','store','display','forward'):
        raise ValueError('Unknown source capability')
    row = db.query_one(f'SELECT can_{action} AS allowed FROM source WHERE source_id=?', (source_id,))
    return bool(row and row['allowed'])


def event_permitted(db, event_id, action):
    # A derived conclusion can contain information from all contributing sources.
    # Conservatively withhold it if any linked source disallows the operation.
    rows = db.query("""SELECT DISTINCT r.source_id FROM raw_item r
        LEFT JOIN event_source es ON es.raw_item_id=r.raw_item_id
        WHERE r.event_id=? OR es.event_id=?""", (event_id,event_id))
    if not rows:
        event = db.query_one('SELECT first_source_id FROM event WHERE event_id=?', (event_id,))
        return bool(event and event['first_source_id'] and permitted(db,event['first_source_id'],action))
    return all(permitted(db,row['source_id'],action) for row in rows)


def permitted_event_ids(db, event_ids, action):
    if action not in ('fetch','store','display','forward'):
        raise ValueError('Unknown source capability')
    ids = list(dict.fromkeys(event_ids))
    if not ids: return set()
    rows = db.query(f"""SELECT e.event_id, MIN(COALESCE(s.can_{action},fallback.can_{action},0)) AS allowed
        FROM event e LEFT JOIN event_source es ON es.event_id=e.event_id
        LEFT JOIN raw_item r ON r.raw_item_id=es.raw_item_id OR r.event_id=e.event_id
        LEFT JOIN source s ON s.source_id=r.source_id
        LEFT JOIN source fallback ON fallback.source_id=e.first_source_id
        WHERE e.event_id IN ({','.join('?' for _ in ids)}) GROUP BY e.event_id""", tuple(ids))
    return {r['event_id'] for r in rows if r['allowed']}
