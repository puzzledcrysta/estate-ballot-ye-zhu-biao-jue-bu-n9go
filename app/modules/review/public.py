import json
from datetime import datetime, timezone
from app.errors import DataError
from app.store import connection
from app.modules.runs.repository import lookup

# 仅一户投票的议案，公开对账差额会暴露该户选择；
# 参与投票户数达到此下限才在公开版公布数值，零户投票的结果恒为 0 不在此限。
PUBLIC_MIN_VOTERS = 2


def latest_review(db, run_id):
    return db.execute('SELECT version,decision,note,changed FROM reviews WHERE run_id=? '
                      'ORDER BY version DESC LIMIT 1', (run_id,)).fetchone()


def published(db, run_id, review_version):
    return db.execute('SELECT payload,created FROM public_versions WHERE run_id=? AND review_version=?',
                      (run_id, review_version)).fetchone()


def voters_by_key(run):
    voters = {}
    for row in run['accepted']:
        proof = {item['metric']: item['value'] for item in row['proof']}
        voters[row['key']] = int(proof['参与人数'])
    return voters


def redact(run, conclusion, created):
    voters = voters_by_key(run)
    rows, hidden = [], False
    for row in run['book']['rows']:
        count = voters.get(row['key'])
        if count is not None and 0 < count < PUBLIC_MIN_VOTERS:
            hidden = True
            rows.append({'key': row['key'], 'suppressed': True, 'reason': 'HOUSEHOLD_PRIVACY'})
        else:
            rows.append({'key': row['key'], 'amount': row['amount'], 'reference': row['reference'],
                         'delta': row['delta'], 'code': row['code']})
    book = run['book']
    # 有议案被隐去时不出具合计与净差，避免由总额反推单户数值
    reconciliation = {'rows': rows, 'withheld': hidden,
                      'matched': None if hidden else book['matched'],
                      'mismatched': None if hidden else book['mismatched'],
                      'actual': None if hidden else book['actual'],
                      'expected': None if hidden else book['expected'],
                      'delta': None if hidden else book['delta']}
    return {'manifest': run['manifest'], 'counts': run['counts'], 'conclusion': conclusion,
            'reconciliation': reconciliation, 'published': created}


def publish(run_id):
    with connection() as db:
        db.execute('BEGIN IMMEDIATE')
        run = json.loads(lookup(db, run_id)['result'])
        latest = latest_review(db, run_id)
        if latest is None:
            raise DataError('PUBLIC_NOT_READY', '尚无复核结论，不能发布公开版', 409)
        if latest['decision'] != '确认':
            raise DataError('PUBLIC_RETURNED', '当前结论为退回，须重新确认后才能发布', 409)
        existing = published(db, run_id, latest['version'])
        if existing is not None:
            return {'review_version': latest['version'], 'published': existing['created'], 'fresh': False}
        created = datetime.now(timezone.utc).isoformat()
        payload = json.dumps(redact(run, dict(latest), created), ensure_ascii=False, indent=2)
        db.execute('INSERT INTO public_versions VALUES (?,?,?,?)',
                   (run_id, latest['version'], payload, created))
        return {'review_version': latest['version'], 'published': created, 'fresh': True}


def status(run_id):
    with connection() as db:
        lookup(db, run_id)
        latest = latest_review(db, run_id)
        if latest is None:
            return {'state': 'unavailable', 'detail': '尚无复核结论'}
        if latest['decision'] != '确认':
            return {'state': 'unavailable', 'detail': '当前结论为退回，公开版已失效'}
        existing = published(db, run_id, latest['version'])
        if existing is None:
            return {'state': 'unavailable', 'detail': '当前结论尚未发布公开版'}
        return {'state': 'available', 'review_version': latest['version'], 'published': existing['created']}


def download(run_id):
    # 同一事务内判定当前有效结论并取出对应公开版，保证下载与结论一致
    with connection() as db:
        lookup(db, run_id)
        latest = latest_review(db, run_id)
        if latest is None or latest['decision'] != '确认':
            raise DataError('PUBLIC_UNAVAILABLE', '公开版未发布或已失效', 404)
        existing = published(db, run_id, latest['version'])
        if existing is None:
            raise DataError('PUBLIC_UNAVAILABLE', '公开版未发布或已失效', 404)
        return existing['payload'], latest['version']
