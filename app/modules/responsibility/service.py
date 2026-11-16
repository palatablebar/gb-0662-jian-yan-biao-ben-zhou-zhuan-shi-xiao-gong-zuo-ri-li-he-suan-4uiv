"""交接暂停编辑与责任结论切换。

所有会改变责任结论的操作共用一套约束：
- 编辑或删除交接暂停：先做结构校验，再按同一口径重算标本结论与责任用时，
  在同一事务保存；结构非法整体回滚，原交接记录保持可读。
- 重算后责任不平仍保留为证据，但现行责任结论不切换，复核不能形成正式记录。
- 重跑同一核对：按稳定 conclusion_id 幂等，不新增责任记录。
- 更换现行结论：先验证新结论，再切换指针；失败回滚旧版。
"""
from app import store
from app.errors import RuleError
from app.modules.contract.versions import instant
from app.modules.responsibility.allocate import allocate, verify


def attribute(rows, pauses):
    return allocate(rows, pauses)


def check(result):
    return verify(result)


def _validate(handover):
    if not isinstance(handover, dict):
        raise RuleError('HANDOVER', '交接暂停必须为对象')
    for key in ('start', 'end'):
        if not str(handover.get(key, '')).strip():
            raise RuleError('HANDOVER', f'交接暂停缺少 {key}')
    left = instant(handover['start'])
    right = instant(handover['end'])
    if right <= left:
        raise RuleError('PAUSE', '交接暂停结束必须晚于开始')
    if 'approved' in handover and not isinstance(handover['approved'], bool):
        raise RuleError('HANDOVER', '批准标志必须为布尔值')


def _normalize(handover):
    """标本号是暂停规则的索引键，不写回记录本身，保证往返改动得到同一批次摘要。"""
    out = {key: value for key, value in handover.items() if key != 'specimen'}
    out.setdefault('approved', False)
    return out


def _current(run_id):
    bundle = store.latest()
    if not bundle or bundle['manifest']['run_id'] != run_id:
        raise RuleError('NOT_FOUND', '当前批次不存在', 404)
    pauses = bundle.get('model', {}).get('pauses')
    if not isinstance(pauses, dict):
        raise RuleError('PAUSE', '当前批次缺少暂停规则', 409)
    return bundle, pauses


def edit_handover(run_id, handover):
    _validate(handover)
    bundle, pauses = _current(run_id)
    specimen = str(handover.get('specimen', '')).strip()
    handover_id_value = str(handover.get('handover_id', '')).strip()
    if not specimen and handover_id_value:
        # 交接编号唯一，未带标本号时按现有记录反查，避免把改动落到错误标本。
        specimen = next((name for name, items in pauses.items()
                         if any(str(item.get('handover_id', '')).strip() == handover_id_value for item in items)), '')
        handover = dict(handover, specimen=specimen)
    if not specimen:
        raise RuleError('HANDOVER', '交接暂停必须指定标本号')
    handover = _normalize(handover)
    updated = {name: [dict(item) for item in items] for name, items in pauses.items()}
    target = updated.setdefault(specimen, [])
    if handover_id_value:
        for index, item in enumerate(target):
            if str(item.get('handover_id', '')).strip() == handover_id_value:
                target[index] = handover
                break
        else:
            raise RuleError('NOT_FOUND', '交接记录不存在', 404)
    else:
        target.append(handover)
    store.record_handover_event(bundle['manifest']['run_id'], specimen, handover_id_value, 'edit', handover)
    return _recompute(bundle, updated)


def remove_handover(run_id, specimen, handover_id_value):
    bundle, pauses = _current(run_id)
    updated = {name: [dict(item) for item in items] for name, items in pauses.items()}
    target = updated.get(specimen)
    if not target:
        raise RuleError('NOT_FOUND', '交接记录不存在', 404)
    kept = [item for item in target if str(item.get('handover_id', '')).strip() != str(handover_id_value).strip()]
    if len(kept) == len(target):
        raise RuleError('NOT_FOUND', '交接记录不存在', 404)
    if kept:
        updated[specimen] = kept
    else:
        updated.pop(specimen)
    store.record_handover_event(bundle['manifest']['run_id'], specimen, handover_id_value, 'remove')
    return _recompute(bundle, updated)


def _recompute(bundle, pauses):
    from app.pipeline import execute

    raw = store.raw_input(bundle['manifest']['run_id'])
    if raw is None:
        raw = _sample_by_digest(bundle['manifest'].get('sha256', ''))
    if raw is None:
        raise RuleError('INPUT', '无法恢复当前批次原始输入，请重新采信', 409)
    revised = execute(raw, grace=bundle['calculate'].get('grace', '0'), pauses=pauses)
    # 责任不平仍留作证据并成为最新批次，但现行结论指针不切换，复核会被拒绝。
    return store.save(revised, raw)


def _sample_by_digest(digest):
    """旧批次未存原始输入时，用摘要匹配内置样例恢复同一口径。"""
    import hashlib
    from app.config import SAMPLES
    if not digest:
        return None
    for name in ('dirty', 'clean', 'gb18030'):
        data = (SAMPLES / f'{name}.csv').read_bytes()
        if hashlib.sha256(data).hexdigest() == digest:
            return data
    return None


def promote(conclusion_id):
    return store.promote(conclusion_id)


def restore(conclusion_id):
    return store.restore(conclusion_id)


def handovers(run_id=None):
    bundle = store.latest() if run_id is None else store.run(run_id)
    if not bundle:
        raise RuleError('NOT_FOUND', '批次不存在', 404)
    from app.pipeline import ensure_responsibility
    return ensure_responsibility(bundle).get('responsibility', {})
