"""Independent paraphrases and safety boundaries for literal control metadata."""
from copy import deepcopy

import pytest

from app.control_hints import assess, recall_matches, validate_hints


@pytest.fixture
def tools():
    def tool(field, choices, number, maximum, objects, actions, exclude, values, units):
        return {"id": f"control.example.{number}", "input_schema": {
            "type": "object",
            "properties": {field: {"type": "string", "enum": list(choices)},
                           number: {"type": "integer", "minimum": 0, "maximum": maximum}},
            "required": [field, number],
            "x-agent": {
                "recall": {"objects": objects, "actions": actions, "exclude": exclude},
                "relative_tool": "status.example.get_state",
                "required_mentions": [
                    {"field": field, "kind": "choice", "choices": choices,
                     "all": ["全部", "所有", "全车"], "question": "要设置哪个位置？"},
                    {"field": number, "kind": "number", "units": units,
                     "values": values, "question": "要设置为多少？"},
                ],
            },
        }}

    zones = {"driver": ["主驾", "驾驶位", "驾驶员"],
             "passenger": ["副驾", "副驾驶位"], "rear": ["后排"], "all": ["全区域"]}
    windows = {"front_left": ["左前", "主驾"], "front_right": ["右前", "副驾"],
               "rear_left": ["左后", "后排左侧"], "rear_right": ["右后", "后排右侧"], "all": ["整车"]}
    seat = {k: v for k, v in zones.items() if k not in ("all", "rear")}
    seat.update({"rear_left": ["左后", "后排左侧"], "rear_right": ["右后", "后排右侧"]})
    return {
        "windows": tool("window", windows, "open_percent", 100, ["车窗", "窗户"],
                        ["开", "降", "升", "设置"], ["天窗", "座椅"], [
                            {"phrases": ["打开一半", "开一半", "半开", "一半"], "value": 50},
                            {"phrases": ["全部打开", "开到底", "全开"], "value": 100},
                            {"phrases": ["完全关闭", "关严"], "value": 0},
                        ], ["%"]),
        "ventilation": tool("seat", seat, "level", 3, ["座椅", "通风"],
                            ["通风"], ["加热", "按摩", "车窗", "空调"], [
                                {"phrases": ["最高档", "最大档", "最强"], "value": 3},
                                {"phrases": ["最低档", "最小档"], "value": 1},
                                {"phrases": ["关闭", "关掉", "停掉"], "value": 0},
                            ], ["档", "挡"]),
        "fan": tool("zone", zones, "level", 7, ["风量", "风速", "风力"], [],
                    ["座椅", "车窗", "温度"], [
                        {"phrases": ["最高档", "最大档", "最大", "最强"], "value": 7},
                        {"phrases": ["最低档", "最小档", "最小"], "value": 1},
                        {"phrases": ["关闭", "关掉"], "value": 0},
                    ], ["档", "挡"]),
    }


@pytest.mark.parametrize("name,goal,expected", [
    ("windows", "麻烦将左前车窗开到百分之五十", {"window": "front_left", "open_percent": 50}),
    ("windows", "右后窗户开到５０％吧", {"window": "rear_right", "open_percent": 50}),
    ("windows", "主驾车窗开一半", {"window": "front_left", "open_percent": 50}),
    ("windows", "请把后排左侧窗户半开", {"window": "rear_left", "open_percent": 50}),
    ("windows", "副驾车窗开到底", {"window": "front_right", "open_percent": 100}),
    ("windows", "所有车窗全开", {"window": "all", "open_percent": 100}),
    ("windows", "左前车窗下降到百分之二十五", {"window": "front_left", "open_percent": 25}),
    ("ventilation", "麻烦副驾驶位的座椅通风用二挡", {"seat": "passenger", "level": 2}),
    ("ventilation", "主驾通风设成三档吧", {"seat": "driver", "level": 3}),
    ("ventilation", "后排左侧座椅通风来个2档", {"seat": "rear_left", "level": 2}),
    ("ventilation", "右后座椅通风设置为最高档", {"seat": "rear_right", "level": 3}),
    ("ventilation", "把副驾座椅通风开到最小档", {"seat": "passenger", "level": 1}),
    ("ventilation", "关掉主驾座椅通风", {"seat": "driver", "level": 0}),
    ("fan", "驾驶位空调风量给我设成两挡", {"zone": "driver", "level": 2}),
    ("fan", "帮忙把后排风速调整为四档", {"zone": "rear", "level": 4}),
    ("fan", "全车风力开到最大", {"zone": "all", "level": 7}),
    ("fan", "副驾风量调高到三档", {"zone": "passenger", "level": 3}),
    ("fan", "副驾风量调低至二挡", {"zone": "passenger", "level": 2}),
    ("fan", "主驾风量设为3", {"zone": "driver", "level": 3}),
])
def test_heldout_simple_paraphrases(tools, name, goal, expected):
    tool = tools[name]
    validate_hints(tool)
    assert recall_matches(tool, goal) is True
    assert assess(tool, goal) == {"missing": [], "constraints": expected, "relative": False,
                                 "explicit": True, "excluded": False, "single_call_complete": True}


@pytest.mark.parametrize("name,goal,missing", [
    ("windows", "车窗开一半", ["window"]),
    ("windows", "左前车窗打开", ["open_percent"]),
    ("ventilation", "座椅通风设为二档", ["seat"]),
    ("ventilation", "打开副驾座椅通风", ["level"]),
    ("fan", "风量三档", ["zone"]),
    ("fan", "主驾风量调整一下", ["level"]),
])
def test_missing_location_or_level_is_never_defaulted(tools, name, goal, missing):
    result = assess(tools[name], goal)
    assert [m["field"] for m in result["missing"]] == missing
    assert all(m["question"] for m in result["missing"])
    assert not result["explicit"]
    assert not set(missing) & result["constraints"].keys()


@pytest.mark.parametrize("goal,known", [("主驾风量提高一档", True), ("主驾风量增加1挡", True),
                                        ("主驾风量再大一点", False), ("主驾风量减少三档", True),
                                        ("主驾风量往上调一档", True), ("主驾风量加二档", True),
                                        ("主驾风量降低到原来的一半", False),
                                        ("主驾风量开大点", False), ("主驾风量更大", False)])
def test_relative_deltas_are_not_absolute_levels(tools, goal, known):
    result = assess(tools["fan"], goal)
    assert result["relative"] and not result["explicit"]
    assert result["constraints"] == {"zone": "driver"}
    assert result["missing"] == ([] if known else [{"field": "level", "question": "要设置为多少？"}])
    assert not result["single_call_complete"]


def test_relative_without_query_hint_still_requires_clarification(tools):
    del tools["ventilation"]["input_schema"]["x-agent"]["relative_tool"]
    result = assess(tools["ventilation"], "主驾座椅通风调高一档")
    assert result["relative"] and result["missing"] and not result["explicit"]
    assert "level" not in result["constraints"]


@pytest.mark.parametrize("name,goal", [
    ("ventilation", "不要关闭主驾座椅通风"),
    ("ventilation", "副驾通风别关掉"),
    ("ventilation", "不用把主驾通风开到最高档"),
    ("windows", "别把所有车窗全部打开"),
    ("windows", "不是要把左前车窗开一半"),
    ("windows", "请勿把左前车窗开一半"),
    ("ventilation", "不必关闭主驾座椅通风"),
    ("fan", "主驾风量不要设为三档"),
])
def test_negated_requests_never_become_positive_targets(tools, name, goal):
    assert recall_matches(tools[name], goal) is False
    result = assess(tools[name], goal)
    assert result["constraints"] == {}
    assert not result["explicit"] and result["missing"]


@pytest.mark.parametrize("name,goal", [
    ("windows", "左前天窗开一半"),
    ("windows", "座椅旁的车窗开一半"),
    ("ventilation", "副驾座椅加热三档"),
    ("ventilation", "关闭座椅加热打开座椅通风"),
    ("fan", "主驾座椅通风风量三档"),
    ("fan", "主驾空调温度三档"),
])
def test_cross_object_exclusions(tools, name, goal):
    assert recall_matches(tools[name], goal) is False
    assert not assess(tools[name], goal)["explicit"]


@pytest.mark.parametrize("name,goal,field,value", [
    ("windows", "左前车窗开到120%", "open_percent", 120),
    ("windows", "左前车窗开到-5%", "open_percent", -5),
    ("windows", "左前车窗开到百分之一百二十", "open_percent", 120),
    ("ventilation", "副驾座椅通风四档", "level", 4),
    ("ventilation", "副驾座椅通风二点五档", "level", 2.5),
    ("fan", "主驾风量八档", "level", 8),
    ("fan", "主驾风量2.5档", "level", 2.5),
    ("fan", "主驾风量负一档", "level", -1),
    ("fan", "主驾风量9007199254740993档", "level", 9007199254740993),
])
def test_out_of_range_or_fractional_target_is_preserved_not_clamped(tools, name, goal, field, value):
    result = assess(tools[name], goal)
    assert result["constraints"][field] == value
    assert field in [m["field"] for m in result["missing"]]
    assert not result["explicit"]


@pytest.mark.parametrize("goal,expected", [
    ("座椅通风设为二档\n副驾", {"seat": "passenger", "level": 2}),
    ("主驾座椅通风\n三档", {"seat": "driver", "level": 3}),
    ("主驾座椅通风\n二", {"seat": "driver", "level": 2}),
])
def test_caller_combined_user_answers(tools, goal, expected):
    result = assess(tools["ventilation"], goal)
    assert result["constraints"] == expected
    assert result["explicit"]


@pytest.mark.parametrize("reverse", [False, True])
def test_longest_choice_alias_wins_independently_of_mapping_order(tools, reverse):
    tool = tools["ventilation"]
    mention = tool["input_schema"]["x-agent"]["required_mentions"][0]
    if reverse:
        mention["choices"] = dict(reversed(list(mention["choices"].items())))
    result = assess(tool, "座椅通风设为二档\n副驾驶位")
    assert result["constraints"] == {"seat": "passenger", "level": 2}
    assert not result["missing"] and result["single_call_complete"]


@pytest.mark.parametrize("name,goal", [
    ("fan", "全部空调风量二档\n不用全部主驾就好"),
    ("ventilation", "全部座椅通风二档\n不用全部，主驾就好"),
    ("ventilation", "座椅通风二档\n不要全部，只要主驾"),
])
def test_negated_all_in_combined_answer_never_preserves_all_scope(tools, name, goal):
    result = assess(tools[name], goal)
    assert result["constraints"] == {}
    assert result["missing"]
    assert not result["explicit"] and not result["single_call_complete"]


def test_conflicting_choices_and_numbers_require_clarification(tools):
    result = assess(tools["ventilation"], "主驾或副驾座椅通风二档或者三档")
    assert set(result["constraints"]["seat"]) == {"driver", "passenger"}
    assert result["constraints"]["level"] == [2, 3]
    assert len(result["missing"]) == 2 and not result["explicit"]


def test_all_without_an_all_enum_does_not_pick_one_seat(tools):
    result = assess(tools["ventilation"], "全部座椅通风三档")
    assert set(result["constraints"]["seat"]) == {"driver", "passenger", "rear_left", "rear_right"}
    assert result["missing"] == [] and not result["explicit"]
    assert not result["single_call_complete"]


@pytest.mark.parametrize("bound,phrase,expected", [("maximum", "最高档", 7), ("minimum", "最低档", 0)])
def test_schema_bounds_resolve_config_values(tools, bound, phrase, expected):
    hint = tools["fan"]["input_schema"]["x-agent"]["required_mentions"][1]
    hint["values"] = [{"phrases": [phrase], "value": bound}]
    validate_hints(tools["fan"])
    result = assess(tools["fan"], f"主驾风量设为{phrase}")
    assert result["constraints"]["level"] == expected and result["explicit"]


def test_missing_schema_bound_is_invalid_metadata(tools):
    tool = tools["fan"]
    tool["input_schema"]["x-agent"]["required_mentions"][1]["values"][0]["value"] = "maximum"
    del tool["input_schema"]["properties"]["level"]["maximum"]
    with pytest.raises(ValueError, match="numeric constant"):
        validate_hints(tool)


def test_all_targets_cover_schema_not_only_configured_aliases(tools):
    tool = tools["ventilation"]
    del tool["input_schema"]["x-agent"]["required_mentions"][0]["choices"]["rear_right"]
    validate_hints(tool)
    result = assess(tool, "全部座椅通风二档")
    assert result["constraints"]["seat"] == tool["input_schema"]["properties"]["seat"]["enum"]
    assert not result["missing"] and not result["single_call_complete"]


@pytest.mark.parametrize("action,goal", [("打开", "请打开左前车窗"), ("关闭", "麻烦关闭所有车窗")])
def test_dedicated_window_open_close_needs_only_window(tools, action, goal):
    tool = tools["windows"]
    schema = tool["input_schema"]
    del schema["properties"]["open_percent"]
    schema["required"] = ["window"]
    hints = schema["x-agent"]
    hints["required_mentions"] = hints["required_mentions"][:1]
    hints["recall"]["actions"] = [action]
    hints["recall"]["exclude"] += ["%", "百分之", "一半"]
    validate_hints(tool)
    result = assess(tool, goal)
    assert result["explicit"] and not result["missing"]
    assert result["constraints"] == {"window": "front_left" if action == "打开" else "all"}
    assert not recall_matches(tool, "打开左前车窗到50%")
    excluded = assess(tool, "打开左前车窗到50%")
    assert excluded["excluded"] and not excluded["single_call_complete"]
    assert not assess(tool, "不要关闭左前车窗")["explicit"]


def test_absolute_vs_relative_windows(tools):
    assert assess(tools["windows"], "左前车窗降到50%")["explicit"]
    relative = assess(tools["windows"], "左前车窗再开10%")
    assert relative["relative"] and not relative["explicit"]
    assert relative["constraints"] == {"window": "front_left"}
    assert not relative["missing"] and not relative["single_call_complete"]


def test_all_in_numeric_phrase_does_not_change_selected_window(tools):
    result = assess(tools["windows"], "左前车窗全部打开")
    assert result["constraints"] == {"window": "front_left", "open_percent": 100}
    assert result["explicit"]


@pytest.mark.parametrize("value", ["1e3", "2.3.4", "++3", "level3"])
def test_malformed_numbers_are_not_reinterpreted_as_suffixes(tools, value):
    result = assess(tools["fan"], f"主驾风量{value}档")
    assert "level" not in result["constraints"]
    assert not result["explicit"]


def test_overlapping_numeric_phrases_take_specific_mapping(tools):
    hint = tools["windows"]["input_schema"]["x-agent"]["required_mentions"][1]
    hint["values"].append({"phrases": ["开"], "value": 100})
    result = assess(tools["windows"], "左前车窗打开一半")
    assert result["constraints"]["open_percent"] == 50
    assert result["explicit"]


def test_literal_config_is_not_regex_or_subword_match(tools):
    recall = tools["fan"]["input_schema"]["x-agent"]["recall"]
    recall.update(objects=["(a+)+$"], actions=["on"], exclude=[])
    validate_hints(tools["fan"])
    assert recall_matches(tools["fan"], "a" * 10000 + " on") is False
    assert recall_matches(tools["fan"], "(a+)+$ ventilation") is False
    assert recall_matches(tools["fan"], "(a+)+$ ON") is True


def test_defaults_are_unconstrained_and_input_is_not_mutated(tools):
    tool = tools["fan"]
    original = deepcopy(tool)
    assess(tool, "主驾风量三档")
    validate_hints(tool)
    assert tool == original
    del tool["input_schema"]["x-agent"]
    assert assess(tool, "任何目标") is None
    assert recall_matches(tool, "任何目标") is None
    assert validate_hints(tool) is None


def test_uncovered_required_field_cannot_enable_fast_path(tools):
    tools["fan"]["input_schema"]["required"].append("other")
    assert not assess(tools["fan"], "主驾风量三档")["explicit"]


@pytest.mark.parametrize("mutation", [
    lambda h: h.update(regex=".*"),
    lambda h: h.update(relative_tool=3),
    lambda h: h.update(recall={"regex": ".*"}),
    lambda h: h.update(recall={"objects": "车窗"}),
    lambda h: h.update(recall={"objects": ["x" * 129]}),
    lambda h: h.update(recall={"objects": ["x"] * 65}),
    lambda h: h.update(required_mentions=[{}] * 33),
    lambda h: h["required_mentions"][0].update(field="missing"),
    lambda h: h["required_mentions"][0].update(choices={"unknown": ["主驾"]}),
    lambda h: h["required_mentions"][0].update(choices={}),
    lambda h: h["required_mentions"][0].update(choices={"driver": []}),
    lambda h: h["required_mentions"][0].update(kind="regex"),
    lambda h: h["required_mentions"][0].update(units=["度"]),
    lambda h: h["required_mentions"][1].update(field="zone"),
    lambda h: h["required_mentions"][1].update(values=[{"phrases": ["最大"], "value": 8}]),
    lambda h: h["required_mentions"][1].update(values=[{"phrases": ["最大"], "value": True}]),
    lambda h: h["required_mentions"][1].update(values=[{"phrases": ["最大"], "value": float("nan")}]),
    lambda h: h["required_mentions"][1].update(values=[{"phrases": ["最大"], "value": 2.5}]),
    lambda h: h["required_mentions"][1].update(values=[{"phrases": [], "value": 2}]),
    lambda h: h["required_mentions"][1].pop("question"),
])
def test_metadata_validation_rejects_invalid_config(tools, mutation):
    mutation(tools["fan"]["input_schema"]["x-agent"])
    with pytest.raises(ValueError, match="x-agent"):
        validate_hints(tools["fan"])


@pytest.mark.parametrize("hints", [None, [], "hints", 3])
def test_metadata_must_be_object(tools, hints):
    tools["fan"]["input_schema"]["x-agent"] = hints
    with pytest.raises(ValueError, match="x-agent"):
        validate_hints(tools["fan"])


def test_aggregate_literal_limit(tools):
    hints = tools["fan"]["input_schema"]["x-agent"]
    hints["required_mentions"][1]["values"] = [{"phrases": [f"term{i}"] * 64, "value": 1} for i in range(9)]
    with pytest.raises(ValueError, match="too many literals"):
        validate_hints(tools["fan"])


def test_oversized_text_fails_closed(tools):
    goal = "主驾风量三档" + " " * 40000
    assert recall_matches(tools["fan"], goal) is False
    result = assess(tools["fan"], goal)
    assert not result["explicit"] and not result["constraints"]
