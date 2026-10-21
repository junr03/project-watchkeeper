"""SQL relation for raw observations and currently reviewable retained evidence."""


def install_reviewable_observations(connection):
    """Install a live view over raw rows and valid open-project snapshots.

    The view stores no copied content. Its retained side is admitted only when
    the snapshot revision matches the latest expiration record and no current
    raw row exists for that observation ID. Open archived or snoozed projects
    remain eligible for this relation; caller queries decide active visibility.
    """
    connection.execute("""CREATE VIEW IF NOT EXISTS reviewable_observations AS
        SELECT observation_id,source_id,thread_id,message_id,revision,source_timestamp,
               captured_at,locator,text,0 AS is_retained
          FROM observations
        UNION
        SELECT json_extract(item.value,'$.observation_id') AS observation_id,
               json_extract(item.value,'$.source_id') AS source_id,
               json_extract(item.value,'$.thread_id') AS thread_id,
               json_extract(item.value,'$.message_id') AS message_id,
               json_extract(item.value,'$.revision') AS revision,
               json_extract(item.value,'$.source_timestamp') AS source_timestamp,
               json_extract(item.value,'$.captured_at') AS captured_at,
               json_extract(item.value,'$.locator') AS locator,
               json_extract(item.value,'$.text') AS text,
               1 AS is_retained
          FROM projects AS p
          CROSS JOIN json_each(p.evidence_json) AS item
          JOIN expired_observations AS expired
            ON expired.observation_id=json_extract(item.value,'$.observation_id')
           AND expired.revision=json_extract(item.value,'$.revision')
         WHERE p.disposition='open'
           AND expired.rowid=(
               SELECT latest.rowid FROM expired_observations AS latest
                WHERE latest.observation_id=expired.observation_id
                ORDER BY latest.expired_at DESC,latest.first_captured_at DESC,latest.rowid DESC
                LIMIT 1
           )
           AND NOT EXISTS (
               SELECT 1 FROM observations AS current
                WHERE current.observation_id=expired.observation_id
           )
        """)


def retained_evidence(connection, evidence_items):
    """Return input snapshots that the shared SQL relation considers valid.

    This deliberately delegates revision choice and current-row precedence to
    `reviewable_observations`, keeping reviewer validation and bounded paging
    on one authority.
    """
    candidates = {}
    for item in evidence_items:
        if isinstance(item, dict) and isinstance(item.get("observation_id"), str):
            revision = item.get("revision")
            if isinstance(revision, str):
                candidates[(item["observation_id"], revision)] = item
    if not candidates:
        return []
    found = set()
    keys = list(candidates)
    for start in range(0, len(keys), 300):
        chunk = keys[start:start + 300]
        clauses = " OR ".join("(observation_id=? AND revision=?)" for _ in chunk)
        params = tuple(value for key in chunk for value in key)
        found.update((row[0], row[1]) for row in connection.execute(
            f"SELECT observation_id,revision FROM reviewable_observations WHERE is_retained=1 AND ({clauses})",
            params))
    return [item for key, item in candidates.items() if key in found]
