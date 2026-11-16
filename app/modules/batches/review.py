from app.errors import RuleError
from app.modules.responsibility.allocate import verify


def check(bundle):
    if not bundle['reconcile']['balanced']:
        raise RuleError('UNBALANCED', '行数不平，请重新采信', 409)
    # 责任对不平的批次不能形成正式记录：先核对责任恒等式，再允许复核。
    verify(bundle.get('responsibility'))
    return True
