import threading
import pytest


@pytest.fixture
def anchor(client):
    return client.post('/api/runs', json={}).json()['manifest']['run_id']


def decide(client, anchor, version=0, decision='确认', note='已逐条核对外部账'):
    return client.post(f'/api/review/{anchor}',
                       json={'version': version, 'decision': decision, 'note': note})


def publish(client, anchor):
    return client.post(f'/api/review/{anchor}/publish')


def test_publish_requires_confirmed_conclusion(client, anchor):
    response = publish(client, anchor)
    assert (response.status_code, response.json()['code']) == (409, 'PUBLIC_NOT_READY')
    assert decide(client, anchor, decision='退回', note='缺来源旁证').status_code == 200
    response = publish(client, anchor)
    assert (response.status_code, response.json()['code']) == (409, 'PUBLIC_RETURNED')


def test_public_download_drops_source_ballots(client, anchor):
    assert decide(client, anchor).status_code == 200
    assert publish(client, anchor).json()['fresh'] is True
    response = client.get(f'/api/export/{anchor}/public')
    assert response.status_code == 200 and 'attachment' in response.headers['content-disposition']
    body = response.json()
    assert set(body) == {'manifest', 'counts', 'conclusion', 'reconciliation', 'published'}
    assert body['conclusion']['decision'] == '确认' and body['conclusion']['version'] == 1
    assert body['reconciliation']['rows'] and not body['reconciliation']['withheld']
    assert all('raw' not in row and 'line' not in row for row in body['reconciliation']['rows'])


def test_retry_publish_adds_no_version(client, anchor):
    assert decide(client, anchor).status_code == 200
    first, second = publish(client, anchor).json(), publish(client, anchor).json()
    assert first['review_version'] == second['review_version'] == 1
    assert first['fresh'] and not second['fresh'] and first['published'] == second['published']
    assert client.get(f'/api/export/{anchor}/public').status_code == 200


def test_reject_invalidates_public_immediately(client, anchor):
    assert decide(client, anchor).status_code == 200
    publish(client, anchor)
    assert client.get(f'/api/export/{anchor}/public').status_code == 200
    assert decide(client, anchor, 1, '退回', '请补来源旁证').status_code == 200
    gone = client.get(f'/api/export/{anchor}/public')
    assert (gone.status_code, gone.json()['code']) == (404, 'PUBLIC_UNAVAILABLE')
    assert client.get(f'/api/review/{anchor}/public').json()['state'] == 'unavailable'
    internal = client.get(f'/api/export/{anchor}')
    assert internal.status_code == 200 and internal.json()['results'][0]['raw']


def test_new_conclusion_needs_republish(client, anchor):
    assert decide(client, anchor).status_code == 200
    publish(client, anchor)
    assert decide(client, anchor, 1, '确认', '再次确认无误').status_code == 200
    assert client.get(f'/api/export/{anchor}/public').status_code == 404
    assert publish(client, anchor).json()['review_version'] == 2
    body = client.get(f'/api/export/{anchor}/public').json()
    assert body['conclusion']['version'] == 2 and body['conclusion']['note'] == '再次确认无误'


def test_racing_reviewers_download_matches_winner(client, anchor):
    first = decide(client, anchor, 0, '确认', '甲计票员核对无误')
    second = decide(client, anchor, 0, '确认', '乙计票员核对无误')
    assert first.status_code == 200 and second.status_code == 409
    publish(client, anchor)
    body = client.get(f'/api/export/{anchor}/public').json()
    assert body['conclusion']['version'] == 1 and body['conclusion']['note'] == '甲计票员核对无误'


def test_concurrent_publish_creates_one_version(client, anchor):
    assert decide(client, anchor).status_code == 200
    outcomes = []
    threads = [threading.Thread(target=lambda: outcomes.append(publish(client, anchor).json()))
               for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert {item['review_version'] for item in outcomes} == {1}
    assert sum(item['fresh'] for item in outcomes) == 1


SINGLE = '''表决议案,修订号,提交时间,议案类别,产权单元,表决票,原始备注
A1,1,2026-11-18T09:30:00+08:00,一般,甲房:90:甲=100;乙房:10:乙=100,甲:赞成,单户
B2,1,2026-11-18T09:30:00+08:00,一般,甲房:90:甲=100;乙房:10:乙=100,甲:赞成;乙:反对,双户
C3,1,2026-11-18T09:30:00+08:00,一般,甲房:90:甲=100;乙房:10:乙=100,无,无人投票
'''
BOOK = '''表决议案,外部记录值(平方米)
A1,90
B2,90
C3,0
'''


def test_single_household_figures_withheld(client):
    run_id = client.post('/api/runs', json={'data': SINGLE, 'book': BOOK}).json()['manifest']['run_id']
    assert decide(client, run_id).status_code == 200
    publish(client, run_id)
    body = client.get(f'/api/export/{run_id}/public').json()
    rows = {row['key']: row for row in body['reconciliation']['rows']}
    assert rows['A1'] == {'key': 'A1', 'suppressed': True, 'reason': 'HOUSEHOLD_PRIVACY'}
    assert rows['B2']['amount'] == '90' and rows['B2']['code'] == 'MATCH'
    assert rows['C3']['amount'] == '0'
    assert body['reconciliation']['withheld'] is True
    for total in ('matched', 'mismatched', 'actual', 'expected', 'delta'):
        assert body['reconciliation'][total] is None
    internal = client.get(f'/api/export/{run_id}').json()
    amounts = {row['key']: row['amount'] for row in internal['results']}
    assert amounts['A1'] == '90' and internal['review']
