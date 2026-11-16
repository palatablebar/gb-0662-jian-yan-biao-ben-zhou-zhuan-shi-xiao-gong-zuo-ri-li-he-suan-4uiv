"""交接等待的责任归属。

裁决：重叠的交接等待由实际交接双方共同承担；没有重叠的等待由实际批准该段
的科室承担。理由：同一段等待被前后科室重复批准时，整段只从标本净用时扣除
一次；若责任仍全部记在申请科室，科室用时无法解释。改为由覆盖该段的实际交
接科室均摊，既不重复扣减标本净用时，也能逐段说明每个科室承担的等待。

规则：

1. 已批准交接记录先合并成等待并集，并与该标本的可计时窗口求交。合并后的
   等待只从标本净用时扣除一次。
2. 把等待并集切成互不重叠的最小段。每一段由覆盖该段的实际批准科室共同承
   担，按科室数均分；只有一个科室覆盖的小段由该科室承担。各科室分摊份额之
   和精确等于标本暂停扣除。
3. 标本净用时仍归该标本记录的科室（申请科室）。申请科室的责任用时为净用时
   加其分摊等待；交接科室的责任用时为其分摊等待。
4. 未批准交接记录不扣减、不分摊，但原始记录完整保留在 handovers 中，供失败
   回滚与追溯使用。
5. 若已批准交接记录缺少科室、交接双方不成立、或分摊结果无法解释，则形成未
   归属分钟并判责任不平；责任不平的复核不能形成正式记录。

恒等式：

    各科室责任净用时合计 = 标本净用时
    各科室分摊交接等待合计 = 标本暂停扣除
    各科室责任用时合计 = 标本净用时 + 标本暂停扣除

科室的 joint_minutes 表示该科室参与批准的完整等待时长，用于说明“共同承担”
的暴露面；它不参与上述恒等式，避免同一段等待影响标本净用时两次。
"""
import hashlib
import json
from collections import defaultdict
from decimal import Decimal

from app.errors import RuleError
from app.modules.contract.versions import instant
from app.modules.model.calendar import intersection

POLICY = {
    'code': 'SHARED_HANDOVER',
    'title': '重叠交接等待由实际交接双方共同承担',
    'decision': '重叠等待不归单一科室，也不重复扣除；由覆盖该段的实际交接科室共同承担，合并等待只扣一次。',
    'rule': '合并等待切分为互不重叠的最小段，每段由覆盖它的实际交接科室均分；净用时仍归标本记录科室。',
    'identities': [
        '各科室责任净用时合计 = 标本净用时',
        '各科室分摊交接等待合计 = 标本暂停扣除',
        '各科室责任用时合计 = 标本净用时 + 标本暂停扣除',
    ],
}

# 兼容常见字段写法，避免因导出字段名不同而把责任错记到申请科室。
_PARTY_KEYS = ('departments', 'parties', 'handover_departments')
_FROM_KEYS = ('from_department', 'handover_from', 'source_department', 'from')
_TO_KEYS = ('to_department', 'handover_to', 'target_department', 'to', 'counterpart')
_APPROVER_KEYS = ('department', 'approved_by', 'approver_department', 'handover_department')


def _text(value):
    return '' if value is None else str(value).strip()


def handover_parties(pause):
    """返回该条交接记录上可解释的科室；缺失时返回空列表。"""
    for key in _PARTY_KEYS:
        value = pause.get(key)
        if isinstance(value, (list, tuple)):
            return [_text(item) for item in value if _text(item)]
    left = next((_text(pause[key]) for key in _FROM_KEYS if _text(pause.get(key))), '')
    right = next((_text(pause[key]) for key in _TO_KEYS if _text(pause.get(key))), '')
    if left or right:
        return [party for party in (left, right) if party]
    return [_text(pause[key]) for key in _APPROVER_KEYS if _text(pause.get(key))]


def handover_id(pause, specimen, index):
    return _text(pause.get('handover_id') or pause.get('id') or f'{specimen}#{index}')


def pause_digest(pauses):
    canonical = json.dumps(pauses, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _minutes(start, end):
    return Decimal(str((end - start).total_seconds())) / 60


def _split(duration, names):
    """按排序后的科室确定性均分，最后一个科室吸收舍入尾差，保证合计精确。"""
    shares = {}
    allocated = Decimal(0)
    count = Decimal(len(names))
    for index, name in enumerate(names):
        share = duration - allocated if index == len(names) - 1 else duration * Decimal(index + 1) / count - allocated
        shares[name] = share
        allocated += share
    return shares


def _issue(specimen, handover, code, message):
    return {
        'specimen': specimen,
        'handover_id': handover.get('handover_id') if handover else None,
        'code': code,
        'message': message,
        'raw': handover.get('raw') if handover else None,
    }


def _effective(pause, work):
    """把一条交接记录裁剪到实际计时窗口，返回有效等待段。"""
    left = instant(pause['start'])
    right = instant(pause['end'])
    if right <= left:
        return []
    hits = []
    for window in work:
        hit = intersection(window, (left, right))
        if hit:
            hits.append(hit)
    return hits


def allocate_specimen(row, pauses):
    specimen = row['specimen']
    applicant = row['department']
    work = sorted((instant(window['start']), instant(window['end'])) for window in row['windows'])

    effective = []
    handovers = []
    issues = []
    for index, pause in enumerate(pauses.get(specimen, []) or []):
        if not isinstance(pause, dict):
            raise RuleError('PAUSE', '暂停记录必须为对象')
        parties = sorted({party for party in handover_parties(pause) if party})
        approved = bool(pause.get('approved'))
        record = {
            'specimen': specimen,
            'handover_id': handover_id(pause, specimen, index),
            'approved': approved,
            'parties': parties,
            # 至少两个不同科室才能证明是交接等待；单方或空声明无法解释责任。
            'valid': len(parties) >= 2,
            'raw': pause,
            'segments': [],
            'effective_minutes': '0',
        }
        hits = _effective(pause, work) if approved else []
        if hits and not record['valid']:
            issues.append(_issue(specimen, record, 'HANDOVER', '交接记录未声明两个不同的交接科室，等待无法归属'))
        for hit in hits:
            effective.append(dict(record, interval=hit))
        record['segments'] = [
            {'start': start.isoformat(), 'end': end.isoformat(), 'minutes': str(_minutes(start, end))}
            for start, end in hits
        ]
        record['effective_minutes'] = str(sum((_minutes(start, end) for start, end in hits), Decimal(0)))
        handovers.append(record)

    # 等待分摊：按互不重叠的最小段，由覆盖该段的实际交接科室均分。
    handover = defaultdict(Decimal)
    joint = defaultdict(Decimal)
    detail = defaultdict(list)
    points = sorted({point for item in effective for point in item['interval']})
    for start, end in zip(points, points[1:]):
        if end <= start:
            continue
        covering = [item for item in effective if item['interval'][0] <= start and end <= item['interval'][1]]
        parties = sorted({party for item in covering for party in item['parties']})
        if not parties:
            continue
        duration = _minutes(start, end)
        for party in parties:
            joint[party] += duration
        for party, share in _split(duration, parties).items():
            handover[party] += share
            detail[party].append({
                'start': start.isoformat(),
                'end': end.isoformat(),
                'minutes': str(share),
                'duration': str(duration),
                'participants': parties,
                'handover_ids': sorted({item['handover_id'] for item in covering}),
            })

    net = Decimal(row['elapsed_minutes'])
    pause = Decimal(row['pause_minutes'])
    allocated = sum(handover.values(), Decimal(0))
    unattributed = pause - allocated
    if unattributed != 0:
        issues.append(_issue(specimen, None, 'RESPONSIBILITY_GAP', f'交接等待未归属 {unattributed} 分钟，与标本暂停扣除不一致'))

    departments = sorted(set(handover) | {applicant})
    records = []
    for department in departments:
        base = net if department == applicant else Decimal(0)
        wait = handover.get(department, Decimal(0))
        records.append({
            'specimen': specimen,
            'department': department,
            'role': '申请科室' if department == applicant else '交接科室',
            'net_minutes': str(base),
            'handover_minutes': str(wait),
            'joint_minutes': str(joint.get(department, Decimal(0))),
            'total_minutes': str(base + wait),
            'handover_detail': detail.get(department, []),
        })

    held_total = sum((Decimal(record['net_minutes']) for record in records), Decimal(0))
    responsible_total = sum((Decimal(record['total_minutes']) for record in records), Decimal(0))
    gross = net + pause
    if held_total != net:
        issues.append(_issue(specimen, None, 'RESPONSIBILITY_GAP', f'责任净用时合计与标本净用时相差 {net - held_total} 分钟'))

    summary = {
        'specimen': specimen,
        'department': applicant,
        'net_minutes': str(net),
        'pause_minutes': str(pause),
        'gross_minutes': str(gross),
        'attributed_handover_minutes': str(allocated),
        'unattributed_minutes': str(max(Decimal(0), unattributed)),
        'responsibility_net_total': str(held_total),
        'responsibility_total': str(responsible_total),
        'gap_minutes': str(gross - responsible_total),
        'responsibility_text': ' / '.join(
            f"{record['department']} {record['total_minutes']}" for record in records
        ),
        'handover_text': ' / '.join(
            f"{record['department']} {record['handover_minutes']}"
            for record in records if Decimal(record['handover_minutes']) != 0
        ) or '无',
        'balanced': (
            not issues
            and held_total == net
            and allocated == pause
            and responsible_total == gross
        ),
        'departments': [
            {
                'department': record['department'],
                'role': record['role'],
                'net_minutes': record['net_minutes'],
                'handover_minutes': record['handover_minutes'],
                'joint_minutes': record['joint_minutes'],
                'total_minutes': record['total_minutes'],
            }
            for record in records
        ],
        'issues': issues,
    }
    return records, handovers, summary


def conclusion_id(result):
    """同样交接暂停与同样责任结论产生稳定标识，支撑幂等重跑与旧版回滚。"""
    canonical = {
        'policy': result['policy']['code'],
        'pause': result['pause_digest'],
        'rows': [
            [item['specimen'], item['department'], item['role'],
             item['net_minutes'], item['handover_minutes'], item['total_minutes']]
            for item in result['rows']
        ],
        'specimens': [
            [item['specimen'], item['net_minutes'], item['pause_minutes'],
             item['gap_minutes'], item['balanced']]
            for item in result['specimens']
        ],
        'balanced': result['balanced'],
    }
    encoded = json.dumps(canonical, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()
    return hashlib.sha256(encoded).hexdigest()[:20]


def allocate(rows, pauses):
    if not isinstance(pauses, dict):
        raise RuleError('PAUSE', '暂停规则必须为按标本号索引的对象')
    records = []
    handovers = []
    summaries = []
    for row in rows:
        row_records, row_handovers, summary = allocate_specimen(row, pauses)
        records.extend(row_records)
        handovers.extend(row_handovers)
        summaries.append(summary)
    issues = [issue for summary in summaries for issue in summary['issues']]

    departments = {}
    for record in records:
        item = departments.setdefault(record['department'], {
            'department': record['department'],
            'specimens': set(),
            'net_minutes': Decimal(0),
            'handover_minutes': Decimal(0),
            'joint_minutes': Decimal(0),
            'total_minutes': Decimal(0),
        })
        item['specimens'].add(record['specimen'])
        item['net_minutes'] += Decimal(record['net_minutes'])
        item['handover_minutes'] += Decimal(record['handover_minutes'])
        item['joint_minutes'] += Decimal(record['joint_minutes'])
        item['total_minutes'] += Decimal(record['total_minutes'])

    department_rows = [{
        'department': name,
        'specimens': len(item['specimens']),
        'net_minutes': str(item['net_minutes']),
        'handover_minutes': str(item['handover_minutes']),
        'joint_minutes': str(item['joint_minutes']),
        'total_minutes': str(item['total_minutes']),
    } for name, item in sorted(departments.items())]

    net_total = sum((Decimal(summary['net_minutes']) for summary in summaries), Decimal(0))
    pause_total = sum((Decimal(summary['pause_minutes']) for summary in summaries), Decimal(0))
    gross_total = net_total + pause_total
    net_allocated = sum((Decimal(record['net_minutes']) for record in records), Decimal(0))
    handover_total = sum((Decimal(record['handover_minutes']) for record in records), Decimal(0))
    responsible_total = sum((Decimal(record['total_minutes']) for record in records), Decimal(0))
    gap = gross_total - responsible_total
    balanced = (
        not issues
        and net_allocated == net_total
        and handover_total == pause_total
        and responsible_total == gross_total
        and all(summary['balanced'] for summary in summaries)
    )

    result = {
        'policy': POLICY,
        'rows': records,
        'specimens': summaries,
        'departments': department_rows,
        'handovers': handovers,
        'net_total': str(net_total),
        'pause_total': str(pause_total),
        'gross_total': str(gross_total),
        'responsibility_net_total': str(net_allocated),
        'handover_attributed_total': str(handover_total),
        'responsibility_total': str(responsible_total),
        'gap_minutes': str(gap),
        'balanced': balanced,
        'issues': issues,
        'pause_digest': pause_digest(pauses),
    }
    result['conclusion_id'] = conclusion_id(result)
    return result


def verify(responsibility):
    """复核前再次核对责任恒等式，防止陈旧或被篡改的结论进入正式记录。"""
    if not isinstance(responsibility, dict) or not responsibility.get('rows'):
        raise RuleError('RESPONSIBILITY', '缺少责任结论，请重新核算', 409)
    net = sum((Decimal(item['net_minutes']) for item in responsibility['specimens']), Decimal(0))
    pause = sum((Decimal(item['pause_minutes']) for item in responsibility['specimens']), Decimal(0))
    held = sum((Decimal(item['net_minutes']) for item in responsibility['rows']), Decimal(0))
    wait = sum((Decimal(item['handover_minutes']) for item in responsibility['rows']), Decimal(0))
    total = sum((Decimal(item['total_minutes']) for item in responsibility['rows']), Decimal(0))
    if (
        held != net
        or wait != pause
        or total != net + pause
        or Decimal(responsibility.get('gap_minutes', '0')) != 0
        or not responsibility.get('balanced')
    ):
        raise RuleError('RESPONSIBILITY_UNBALANCED', '责任用时不平，请重新核对交接暂停', 409)
    return True
