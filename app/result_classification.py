"""Classify validated success evidence; do not infer whether every user goal is fulfilled."""
SUCCESS_OUTCOMES = {'auto', 'prepared', 'answered', 'mixed'}


def resolve_success_outcome(requested, controls, queries, errors, unresolved):
    if requested not in SUCCESS_OUTCOMES:
        return None
    if errors or unresolved:
        raise ValueError('仍有引用的错误或未完成项，不能报告全部成功')
    if not controls and not queries:
        raise ValueError('没有可用于完成任务的成功回执')
    # A wrong label may include extra successful evidence. It cannot stand in
    # for an operation that was claimed but has no successful receipt at all.
    if requested in {'prepared', 'mixed'} and not controls:
        raise ValueError('声明了车控完成，但没有成功的车控回执')
    if requested in {'answered', 'mixed'} and not queries:
        raise ValueError('声明了查询完成，但没有成功的查询回执')
    outcome = 'mixed' if controls and queries else 'prepared' if controls else 'answered'
    return {'requested': requested, 'resolved': outcome, 'source': 'validated_receipts',
            'control_results': len(controls), 'query_results': len(queries),
            'label_changed': requested not in {'auto', outcome}}
