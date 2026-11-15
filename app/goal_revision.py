"""Bounded, explicit temperature edits. Not a general natural-language goal parser.

Tool arguments remain validated against the selected catalog snapshot. Unknown or
compound changes pause for clarification instead of guessing or rewriting outputs.
"""
import re
from jsonschema import Draft202012Validator

TOOL = 'control.climate.set_temperature'
NUM = r'(?:\d+(?:\.\d+)?|[零一二两三四五六七八九十]+)'
TEMP = re.compile('('+NUM+r')\s*(?:摄氏)?度')
ZONES = {'driver': ('主驾',), 'passenger': ('副驾',), 'rear': ('后排',), 'all': ('所有分区', '所有区域', '全部', '全车')}


def number(s):
    try:
        return float(s)
    except ValueError:
        digits = dict(zip('零一二三四五六七八九', range(10))); digits['两'] = 2
        if '十' in s:
            a, b = s.split('十')
            return (digits[a] if a else 1)*10+(digits[b] if b else 0)
        return digits[s]


def zone(text):
    found = {key for key, names in ZONES.items() if any(n in text for n in names)}
    if len(found) > 1:
        raise ValueError('请确认要修改的单个分区或全部分区。')
    return next(iter(found), None)


def prepare_change(task, text, snapshot):
    goal = task['goal']
    # An intentionally narrow first release: one absolute-temperature request.
    original = TEMP.findall(goal)
    if '空调' not in goal or len(original) != 1 or re.search(r'然后|并且|同时|再查|查看|打开|关闭|车窗|天窗|座椅|风量|风向|增加|降低|升高', goal):
        raise ValueError('当前改口支持单项空调目标温度。请明确完整目标和分区，或取消后另建任务。')
    original_residue = TEMP.sub('', goal)
    original_residue = re.sub(r'请|帮我|把|将|空调|温度|设置为|设置到|设置成|设为|设到|设成|调到|调成|调整到|调整为|开到|所有分区|所有区域|全部|全车|主驾|副驾|后排|的|了|吧|[\s，。！,!.、]', '', original_residue)
    if original_residue or re.search(r'不要|不用|不想|不改|别', text):
        raise ValueError('原目标包含其他要求或修改是否定表达，请明确完整目标；不会丢弃原有要求。')
    # Strip a bounded vocabulary; remaining words mean the edit needs clarification.
    residue = TEMP.sub('', text)
    residue = re.sub(r'请|帮我|还是|算了|不要|不用|改成|改为|改到|改|换成|设置为|设置到|设为|设到|调到|调整到|调整为|原来|现在|从|把|将|空调|温度|所有分区|所有区域|全部|全车|主驾|副驾|后排|的|了|吧|[\s，。！,!.、]', '', residue)
    values = TEMP.findall(text)
    if residue or len(values) not in (1, 2):
        raise ValueError('请明确新温度，例如“改成21度”，以及需要的分区。')
    if len(values) == 2 and number(values[0]) != number(original[0]):
        raise ValueError('提到的原温度与当前目标不同，请确认要修改的任务。')
    target = number(values[-1])
    selected_zone = zone(text) or zone(goal)
    if not selected_zone:
        for entry in reversed(task['history']):
            selected_zone = zone(entry.get('answer', ''))
            if selected_zone:
                break
    if not selected_zone:
        raise ValueError('要改到新温度的哪个分区：主驾、副驾、后排还是全部？请一起说明温度。')
    tool = snapshot.tools.get(TOOL)
    arguments = {'zone': selected_zone, 'temperature_c': target}
    if not tool or not tool['enabled'] or not Draft202012Validator(tool['input_schema']).is_valid(arguments):
        raise ValueError('新温度或分区不符合当前工具参数范围，请重新说明。')
    label = {'driver':'主驾','passenger':'副驾','rear':'后排','all':'所有分区'}[selected_zone]
    return f'把{label}空调设为{target:g}度', {'tool_id': TOOL, 'arguments': arguments}
