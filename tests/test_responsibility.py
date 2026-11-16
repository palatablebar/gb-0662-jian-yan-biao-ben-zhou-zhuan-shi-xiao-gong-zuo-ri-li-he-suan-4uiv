import copy
import json

import pytest

from app import store
from app.config import SAMPLES
from app.errors import RuleError
from app.modules.responsibility.allocate import allocate, verify


def _pauses():
    return json.loads((SAMPLES / 'pauses.json').read_text(encoding='utf-8'))


def _row(result, specimen, department):
    return next(r for r in result['rows'] if r['specimen'] == specimen and r['department'] == department)


def test_overlap_shared_by_handover_parties(clean):
    result = clean['responsibility']
    assert result['policy']['code'] == 'SHARED_HANDOVER'
    applicant = _row(result, '000006', '住院')
    receiver = _row(result, '000006', '检验科')
    # 合并等待 30 分钟只扣一次，由实际交接双方各承担 15 分钟。
    assert result['pause_total'] == '210.0'
    assert applicant['handover_minutes'] == '15.0'
    assert receiver['handover_minutes'] == '15.0'
    assert applicant['net_minutes'] == '90.0'
    assert applicant['total_minutes'] == '105.0'
    assert receiver['total_minutes'] == '15.0'


def test_net_time_allocated_once(clean):
    result = clean['responsibility']
    net = sum(float(item['net_minutes']) for item in result['specimens'])
    held = sum(float(item['net_minutes']) for item in result['rows'])
    elapsed = sum(float(row['elapsed_minutes']) for row in clean['calculate']['rows'])
    # 责任净用时合计等于标本净用时；同一段等待只影响标本净用时一次。
    assert net == held == float(result['net_total']) == elapsed


def test_responsibility_identities(clean):
    result = clean['responsibility']
    assert result['balanced'] is True
    assert result['gap_minutes'] == '0.0'
    assert float(result['responsibility_net_total']) == float(result['net_total'])
    assert float(result['handover_attributed_total']) == float(result['pause_total'])
    assert float(result['responsibility_total']) == float(result['net_total']) + float(result['pause_total'])
    verify(result)


def test_department_totals_add_up(clean):
    result = clean['responsibility']
    assert sum(float(d['net_minutes']) for d in result['departments']) == float(result['net_total'])
    assert sum(float(d['handover_minutes']) for d in result['departments']) == float(result['pause_total'])
    assert sum(float(d['total_minutes']) for d in result['departments']) == float(result['responsibility_total'])


def test_urgent_specimen_split(clean):
    result = clean['responsibility']
    summary = next(s for s in result['specimens'] if s['specimen'] == '000030')
    assert summary['net_minutes'] == '2490.0'
    assert summary['pause_minutes'] == '30.0'
    assert summary['responsibility_total'] == '2520.0'
    assert summary['balanced'] is True


def test_unapproved_handover_retained_not_billed(clean):
    result = clean['responsibility']
    handovers = [h for h in result['handovers'] if h['handover_id'] == '000006-1']
    assert handovers and handovers[0]['approved'] is True
    assert handovers[0]['raw']['from_department'] == '住院'
    # 原始交接记录完整保留，包括未批准与重叠记录。
    assert len(result['handovers']) == 14
    original = result['handovers'][0]['raw']
    assert 'start' in original and 'end' in original and 'approved' in original


def test_unapproved_only_is_zero_but_kept(clean):
    pauses = {'000006': [{'start': '2026-11-02T16:00:00+08:00', 'end': '2026-11-02T17:00:00+08:00', 'approved': False, 'from_department': '住院', 'to_department': '检验科', 'handover_id': 'h-1'}]}
    # 责任归属以同一口径重算后的模型行为准，不能混用旧暂停结论。
    from app.pipeline import execute, sample_bytes
    result = execute(sample_bytes('clean'), pauses=pauses)['responsibility']
    summary = next(s for s in result['specimens'] if s['specimen'] == '000006')
    assert summary['pause_minutes'] == '0'
    assert summary['balanced'] is True
    assert [h['handover_id'] for h in result['handovers'] if h['specimen'] == '000006'] == ['h-1']


def test_missing_parties_unbalanced(clean):
    pauses = _pauses()
    pauses['000006'][0].pop('to_department')
    result = allocate(clean['calculate']['rows'], pauses)
    assert result['balanced'] is False
    assert any(i['code'] == 'HANDOVER' for i in result['issues'])
    with pytest.raises(RuleError):
        verify(result)


def test_three_parties_split_evenly(clean):
    pauses = _pauses()
    pauses['000006'][0]['departments'] = ['住院', '检验科', '门诊']
    result = allocate(clean['calculate']['rows'], pauses)
    summary = next(s for s in result['specimens'] if s['specimen'] == '000006')
    shares = {d['department']: float(d['handover_minutes']) for d in summary['departments']}
    # 前 15 分钟三方均分（各 5）；后 15 分钟同样覆盖三方（各 5），合计各 10。
    assert shares['住院'] == pytest.approx(10.0)
    assert shares['检验科'] == pytest.approx(10.0)
    assert shares['门诊'] == pytest.approx(10.0)
    assert sum(shares.values()) == pytest.approx(float(summary['pause_minutes']))
    assert summary['balanced'] is True


def test_identical_rerun_is_idempotent(client):
    first = client.post('/api/runs', json={}).json()
    before = len(store.concl_history())
    second = client.post('/api/runs', json={}).json()
    assert first['manifest']['run_id'] == second['manifest']['run_id']
    assert len(store.concl_history()) == before


def test_handover_edit_recomputes_specimen_and_responsibility(client):
    bundle = store.latest()
    pauses = _pauses()
    edited = copy.deepcopy(pauses['000006'][0])
    edited['end'] = '2026-11-02T17:30:00+08:00'
    edited['start'] = '2026-11-02T16:00:00+08:00'
    response = client.post('/api/responsibility/handovers', json={'run_id': bundle['manifest']['run_id'], 'handover': edited})
    assert response.status_code == 200
    updated = response.json()
    summary = next(s for s in updated['responsibility']['specimens'] if s['specimen'] == '000006')
    row = next(r for r in updated['model']['rows'] if r['specimen'] == '000006')
    original = next(r for r in bundle['model']['rows'] if r['specimen'] == '000006')
    # 交接暂停改动后标本结论与责任用时同步重算，且仍满足恒等式。
    assert row['pause_minutes'] != original['pause_minutes']
    assert summary['pause_minutes'] == row['pause_minutes']
    assert summary['net_minutes'] == row['elapsed_minutes']
    assert updated['responsibility']['balanced'] is True
    assert updated['manifest']['run_id'] != bundle['manifest']['run_id']


def test_unbalanced_edit_keeps_current_and_blocks_review(client):
    bundle = store.latest()
    current = store.current_conclusion()['conclusion_id']
    broken = copy.deepcopy(_pauses()['000006'][0])
    broken.pop('to_department')
    response = client.post('/api/responsibility/handovers', json={'run_id': bundle['manifest']['run_id'], 'handover': broken})
    assert response.status_code == 200
    updated = response.json()
    assert updated['responsibility']['balanced'] is False
    # 不平的责任只作证据，现行结论不切换。
    assert store.current_conclusion()['conclusion_id'] == current
    review = client.post('/api/batches/review', json={'run_id': updated['manifest']['run_id'], 'version': updated['version']})
    assert review.status_code == 409
    assert review.json()['code'] in ('RESPONSIBILITY_UNBALANCED', 'RESPONSIBILITY_STALE')
    # 原交接记录仍在最新批次里可追溯。
    assert len(updated['responsibility']['handovers']) == 14


def test_promote_failure_restores_old(client):
    good = store.current_conclusion()['conclusion_id']
    bundle = store.latest()
    broken = copy.deepcopy(_pauses()['000006'][0])
    broken.pop('to_department')
    updated = client.post('/api/responsibility/handovers', json={'run_id': bundle['manifest']['run_id'], 'handover': broken}).json()
    bad = updated['responsibility']['conclusion_id']
    with pytest.raises(RuleError):
        store.promote(bad)
    assert store.current_conclusion()['conclusion_id'] == good
    # 旧版仍可恢复，原交接记录不丢。
    store.promote(good)
    assert store.current_conclusion()['conclusion_id'] == good
    assert len(store.conclusion(good)['conclusion']['handovers']) == 14


def test_remove_handover_recomputes(client):
    bundle = store.latest()
    response = client.post('/api/responsibility/handovers/remove', json={'run_id': bundle['manifest']['run_id'], 'specimen': '000006', 'handover_id': '000006-2'})
    assert response.status_code == 200
    updated = response.json()
    row = next(r for r in updated['model']['rows'] if r['specimen'] == '000006')
    # 删除一条重叠批准后仍有一段 30 分钟等待。
    assert row['pause_minutes'] == '30.0'
    assert updated['responsibility']['balanced'] is True


def test_handover_roundtrip_reuses_batch(client):
    base = store.latest()
    rid = base['manifest']['run_id']
    original = _pauses()['000006'][0]
    edited = copy.deepcopy(original)
    edited['end'] = '2026-11-02T17:30:00+08:00'
    once = client.post('/api/responsibility/handovers', json={'run_id': rid, 'handover': edited}).json()
    conclusions = len(store.concl_history())
    # 改回原交接记录后应回到原批次，不再新增责任记录。
    twice = client.post('/api/responsibility/handovers', json={'run_id': once['manifest']['run_id'], 'handover': original}).json()
    assert twice['manifest']['run_id'] == rid
    assert len(store.concl_history()) == conclusions
    assert store.latest()['manifest']['run_id'] == rid


def test_invalid_edit_rolls_back_without_losing_handovers(client):
    bundle = store.latest()
    bad = {'specimen': '000006', 'handover_id': '000006-1', 'start': '2026-11-02T17:00:00+08:00', 'end': '2026-11-02T16:00:00+08:00', 'approved': True, 'from_department': '住院', 'to_department': '检验科'}
    response = client.post('/api/responsibility/handovers', json={'run_id': bundle['manifest']['run_id'], 'handover': bad})
    assert response.status_code == 422
    # 失败不落半批：当前批次与原交接记录保持不变。
    latest = store.latest()
    assert latest['manifest']['run_id'] == bundle['manifest']['run_id']
    assert len(latest['responsibility']['handovers']) == 14
    assert store.current_conclusion()['conclusion_id'] == bundle['responsibility']['conclusion_id']


def test_review_requires_balanced_responsibility(client):
    bundle = store.latest()
    response = client.post('/api/batches/review', json={'run_id': bundle['manifest']['run_id'], 'version': bundle['version']})
    assert response.status_code == 200
    assert response.json()['conclusion_id'] == bundle['responsibility']['conclusion_id']
