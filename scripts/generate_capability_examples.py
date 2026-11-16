"""Create the initial declarative catalog. Never overwrite an existing catalog implicitly."""
import argparse
from copy import deepcopy
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FORMAT = "capability-catalog/v1"


def enum(values, description=""):
    return {"type": "string", "enum": values, **({"description": description} if description else {})}


def number(low, high, integer=True, description=""):
    return {"type": "integer" if integer else "number", "minimum": low, "maximum": high, **({"description": description} if description else {})}


def obj(properties):
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def infer(value):
    if isinstance(value, dict):
        return obj({k: infer(v) for k, v in value.items()})
    if isinstance(value, list):
        return {"type": "array", "items": infer(value[0]) if value else {}}
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, (int, float)):
        return {"type": "number"}
    if value is None:
        return {"type": "null"}
    return {"type": "string"}


def schema_defs():
    string = {"type": "string", "minLength": 1}
    strings = {"type": "array", "items": string, "uniqueItems": True}
    path = {"type": "array", "minItems": 1, "maxItems": 8, "items": {"type": "string", "pattern": "^[a-z][a-z0-9_]*$"}}
    directory = obj({"path": path, "title": string, "description": string, "aliases": strings})
    directory["properties"]["related_query_paths"] = {"type":"array","maxItems":8,"uniqueItems":True,"items":path}
    example = obj({"instruction": string, "arguments": {"type": "object"}})
    mock = {"oneOf": [
        obj({"mode": {"const": "prepare_only"}}),
        obj({"mode": {"const": "static"}, "data": {}}),
        obj({"mode": {"const": "argument_lookup"}, "argument": string, "cases": {"type": "object", "minProperties": 1}}),
        obj({"mode": {"const": "profile_pointer"}, "pointer": {"type": "string", "pattern": "^/"}}),
    ]}
    condition = obj({"pointer": {"type": "string", "pattern": "^/"}, "equals": {}, "code": string, "message": string})
    tool = obj({
        "id": {"type": "string", "pattern": "^[a-z][a-z0-9_]*(\\.[a-z][a-z0-9_]*)+$"},
        "version": {"type": "string", "pattern": "^[0-9]+\\.[0-9]+\\.[0-9]+$"},
        "kind": enum(["control", "manual", "status"]), "category_path": path,
        "title": string, "description": string, "aliases": strings, "enabled": {"type": "boolean"},
        "input_schema": {"type": "object"}, "output_schema": {"type": "object"},
        "mock": mock, "examples": {"type": "array", "minItems": 1, "items": example},
        "preconditions": {"type": "array", "items": condition},
    })
    tool["required"].remove("preconditions")
    profile = obj({"id": string, "title": string, "description": string, "data": {"type": "object"}})
    expected = obj({"outcome": enum(["prepared", "answered", "mixed", "unsupported", "partial", "failed"]), "calls": {"type": "array", "items": obj({"tool_id": string, "arguments": {"type": "object"}})}, "required_domains": {"type": "array", "items": enum(["control", "manual", "status"]), "uniqueItems": True}})
    scenario = obj({"id": string, "kind": enum(["control", "query", "cross_domain"]), "instruction": string, "profile_id": string, "expected": expected})
    package = obj({"name": string, "version": string, "description": string})
    bundle = obj({"format_version": {"const": FORMAT}, "package": package, "directories": {"type": "array", "items": directory}, "tools": {"type": "array", "items": tool}, "profiles": {"type": "array", "minItems": 1, "items": profile}, "scenarios": {"type": "array", "items": scenario}})
    schemas = {"directory": directory, "tool": tool, "profile": profile, "scenario": scenario, "bundle": bundle}
    for name, schema in schemas.items():
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        schema["title"] = f"Vehicle capability {name} v1"
    return schemas


def build():
    categories = {
        "climate": ("空调", "温度、风量、风向、制冷、循环和除霜"),
        "windows": ("车窗", "车窗开度、开关、停止、锁定及一键功能"),
        "seats": ("座椅", "加热、通风、位置、靠背、腰托、按摩与记忆"),
        "multimedia": ("多媒体", "音量、静音、播放、音源、曲目、进度与播放模式"),
        "lights": ("灯光", "氛围灯、阅读灯、大灯和伴我回家照明"),
        "mirrors": ("后视镜", "镜面方向、折叠、加热与自动折叠"),
        "suspension": ("悬架", "空气悬架高度、模式与装载功能"),
        "sunroof": ("天窗", "天窗开度、翘起、遮阳帘与停止"),
    }
    dirs = []
    for domain, title, description in [("control", "车控", "生成车辆操作参数，不执行真实控制"), ("manual", "车书", "查询模拟车型说明、用法和限制"), ("status", "车况", "查询模拟车辆配置和状态快照")]:
        dirs.append({"path": [domain], "title": title, "description": description, "aliases": [title, domain]})
        for category, (label, desc) in categories.items():
            dirs.append({"path": [domain, category], "title": label, "description": desc, "aliases": [label, category]})
    dirs.append({"path": ["status", "vehicle"], "title": "车辆配置与驾驶状态", "description": "核实本车配置、接口可用性和驾驶状态", "aliases": ["车辆配置", "是否配备", "驾驶状态", "vehicle"]})
    tools, scenarios = [], []
    boolean = {"type": "boolean"}
    zone = enum(["driver", "passenger", "rear", "all"], "主驾、副驾、后排、全部")
    seat = enum(["driver", "passenger", "rear_left", "rear_right"], "主驾、副驾、后排左侧、后排右侧")
    window = enum(["front_left", "front_right", "rear_left", "rear_right", "all"], "左前、右前、左后、右后、全部")

    def add(category, action, title, properties, arguments, instruction, description="", schema=None):
        tid = f"control.{category}.{action}"
        tool = {"id": tid, "version": "1.0.0", "kind": "control", "category_path": ["control", category], "title": title,
                "description": (description or title) + "。仅生成调用结构，所有范围为模拟定义，不执行真实车辆控制。",
                "aliases": [title, categories[category][0], action.replace("_", " ")], "enabled": True,
                "input_schema": schema or obj(properties), "output_schema": obj({"prepared": {"const": True}}),
                "mock": {"mode": "prepare_only"}, "examples": [{"instruction": instruction, "arguments": arguments}]}
        if category == "suspension":
            tool["preconditions"] = [
                {"pointer": "/equipment/air_suspension", "equals": True, "code": "NOT_EQUIPPED", "message": "该模拟车辆未配备空气悬架"},
                {"pointer": "/interfaces/suspension", "equals": True, "code": "INTERFACE_UNAVAILABLE", "message": "模拟车辆有配置，但当前未开放悬架接口"},
                {"pointer": "/state/driving/is_parked", "equals": True, "code": "PRECONDITION_NOT_MET", "message": "本模拟悬架接口要求车辆处于驻车状态"},
            ]
        tools.append(tool)
        scenarios.append({"id": f"C{len(scenarios)+1:03}", "kind": "control", "instruction": instruction, "profile_id": "demo-ready", "expected": {"outcome": "prepared", "calls": [{"tool_id": tid, "arguments": arguments}], "required_domains": ["control"]}})

    add("climate", "set_temperature", "设置空调温度", {"zone": zone, "temperature_c": number(16, 30, False, "摄氏度")}, {"zone": "all", "temperature_c": 23}, "把所有分区空调设为23度")
    temperature=tools[-1]
    temperature['version']='1.0.1'
    temperature['examples'].extend({'instruction':f'把{label}空调调到23度','arguments':{'zone':value,'temperature_c':23}} for value,label in [('driver','主驾'),('passenger','副驾'),('rear','后排'),('all','所有分区')])
    add("climate", "set_fan_speed", "设置空调风量", {"zone": zone, "level": number(0, 7)}, {"zone": "driver", "level": 3}, "主驾空调风量设为三档")
    add("climate", "set_airflow", "设置空调风向", {"zone": zone, "direction": enum(["face", "feet", "face_feet", "windshield"], "吹面、吹脚、面脚同吹、吹前挡")}, {"zone": "driver", "direction": "feet"}, "主驾空调改为吹脚")
    add("climate", "set_ac", "空调制冷开关", {"enabled": boolean}, {"enabled": True}, "打开空调AC制冷")
    add("climate", "set_auto", "空调自动模式", {"zone": zone, "enabled": boolean}, {"zone": "all", "enabled": True}, "所有分区空调开启自动模式")
    add("climate", "set_recirculation", "设置空气循环", {"mode": enum(["internal", "external", "auto"], "内循环、外循环、自动")}, {"mode": "internal"}, "空调切换到内循环")
    add("climate", "set_front_defrost", "前挡风玻璃除霜", {"enabled": boolean}, {"enabled": True}, "开启前挡风玻璃除霜")
    add("climate", "set_rear_defrost", "后挡风玻璃除霜", {"enabled": boolean}, {"enabled": True}, "开启后挡风玻璃除霜")
    add("climate", "set_dual_zone", "空调双区独立控制", {"enabled": boolean}, {"enabled": True}, "开启空调双区独立控制")
    add("climate", "set_sync", "空调分区同步", {"enabled": boolean}, {"enabled": True}, "开启空调分区同步")
    add("windows", "set_position", "设置车窗开度", {"window": window, "open_percent": number(0, 100)}, {"window": "front_left", "open_percent": 50}, "左前车窗打开一半")
    add("windows", "open", "完全打开车窗", {"window": window}, {"window": "rear_right"}, "完全打开右后车窗")
    add("windows", "close", "关闭车窗", {"window": window}, {"window": "all"}, "关闭所有车窗")
    add("windows", "stop", "停止车窗运动", {"window": window}, {"window": "front_right"}, "停止右前车窗的升降")
    add("windows", "set_child_lock", "车窗儿童锁", {"enabled": boolean}, {"enabled": True}, "打开车窗儿童锁")
    add("windows", "set_one_touch", "车窗一键升降开关", {"window": window, "enabled": boolean}, {"window": "front_left", "enabled": True}, "启用左前车窗的一键升降功能")
    add("seats", "set_heating", "设置座椅加热", {"seat": seat, "level": number(0, 3)}, {"seat": "driver", "level": 2}, "主驾座椅加热设为二档")
    add("seats", "set_ventilation", "设置座椅通风", {"seat": seat, "level": number(0, 3)}, {"seat": "passenger", "level": 3}, "副驾座椅通风设为三档")
    add("seats", "move", "调节座椅前后位置", {"seat": seat, "direction": enum(["forward", "backward"]), "distance_mm": number(1, 50)}, {"seat": "driver", "direction": "backward", "distance_mm": 20}, "主驾座椅向后移动20毫米")
    add("seats", "set_backrest", "调节座椅靠背", {"seat": seat, "angle_deg": number(90, 130)}, {"seat": "passenger", "angle_deg": 110}, "副驾座椅靠背设为110度")
    add("seats", "set_lumbar", "调节座椅腰托", {"seat": seat, "level": number(0, 5)}, {"seat": "driver", "level": 3}, "主驾座椅腰托调到三档")
    add("seats", "set_massage", "设置座椅按摩", {"seat": seat, "mode": enum(["off", "wave", "pulse"]), "intensity": number(0, 3)}, {"seat": "driver", "mode": "wave", "intensity": 2}, "主驾座椅开启波浪按摩，强度二档")
    add("seats", "save_memory", "保存座椅记忆", {"seat": seat, "slot": number(1, 3)}, {"seat": "driver", "slot": 1}, "将主驾座椅当前位置保存为记忆一")
    add("seats", "recall_memory", "恢复座椅记忆", {"seat": seat, "slot": number(1, 3)}, {"seat": "driver", "slot": 2}, "将主驾座椅恢复到记忆二")
    add("multimedia", "set_volume", "设置媒体音量", {"volume_percent": number(0, 100)}, {"volume_percent": 30}, "媒体音量设为30%")
    add("multimedia", "set_mute", "媒体静音开关", {"enabled": boolean}, {"enabled": True}, "开启媒体静音")
    add("multimedia", "playback", "控制媒体播放", {"action": enum(["play", "pause", "stop", "next", "previous"])}, {"action": "pause"}, "暂停媒体播放")
    add("multimedia", "select_source", "选择媒体音源", {"source": enum(["bluetooth", "radio", "usb", "online"], "蓝牙、收音机、USB、在线音乐")}, {"source": "bluetooth"}, "媒体音源切换到蓝牙")
    add("multimedia", "select_track", "选择示例曲目", {"track_id": enum(["demo_sunny", "demo_rain"], "demo_sunny=模拟晴天；demo_rain=模拟雨天")}, {"track_id": "demo_sunny"}, "选择示例曲目《模拟晴天》作为当前曲目，暂不播放", "只选择当前曲目，不开始播放；播放需另外调用媒体播放工具")
    add("multimedia", "seek", "调整媒体播放进度", {"position_seconds": number(0, 7200)}, {"position_seconds": 90}, "把媒体播放进度跳到90秒")
    add("multimedia", "set_repeat", "设置媒体循环模式", {"mode": enum(["off", "one", "all"], "关闭、单曲、列表循环")}, {"mode": "one"}, "媒体改为单曲循环")
    add("multimedia", "set_shuffle", "媒体随机播放开关", {"enabled": boolean}, {"enabled": True}, "开启媒体随机播放")
    add("lights", "set_ambient_color", "设置氛围灯颜色", {"color": {"type": "string", "pattern": "^#[0-9A-Fa-f]{6}$"}}, {"color": "#0000FF"}, "氛围灯设为蓝色，颜色值#0000FF")
    add("lights", "set_ambient_brightness", "设置氛围灯亮度", {"brightness_percent": number(0, 100)}, {"brightness_percent": 60}, "氛围灯亮度设为60%")
    add("lights", "set_reading", "阅读灯开关", {"position": enum(["front_left", "front_right", "rear", "all"]), "enabled": boolean}, {"position": "front_right", "enabled": True}, "打开右前阅读灯")
    add("lights", "set_headlight_mode", "设置大灯模式", {"mode": enum(["off", "auto", "low_beam", "position"])}, {"mode": "auto"}, "大灯切换为自动模式")
    add("lights", "set_high_beam", "远光灯开关", {"enabled": boolean}, {"enabled": False}, "关闭远光灯")
    add("lights", "set_follow_me_home", "伴我回家照明", {"duration_seconds": number(0, 120)}, {"duration_seconds": 30}, "伴我回家照明设为30秒")
    add("lights", "set_ambient_power", "氛围灯开关", {"enabled": boolean}, {"enabled": True}, "打开氛围灯")
    mirror = enum(["left", "right", "both"])
    add("mirrors", "adjust", "调节后视镜角度", {"mirror": enum(["left", "right"]), "direction": enum(["up", "down", "left", "right"]), "steps": number(1, 5)}, {"mirror": "left", "direction": "down", "steps": 1}, "左后视镜向下调一格")
    add("mirrors", "fold", "后视镜折叠", {"mirror": mirror, "folded": boolean}, {"mirror": "both", "folded": True}, "折叠两侧后视镜")
    add("mirrors", "set_heating", "后视镜加热", {"enabled": boolean}, {"enabled": True}, "开启后视镜加热")
    add("mirrors", "set_auto_fold", "后视镜自动折叠", {"enabled": boolean}, {"enabled": True}, "开启后视镜自动折叠功能")
    suspension_schema = obj({"action": enum(["raise", "lower", "set"]), "height": enum(["low", "normal", "high"])})
    suspension_schema["required"] = ["action"]
    suspension_schema["allOf"] = [{"if": {"properties": {"action": {"const": "set"}}}, "then": {"required": ["height"]}, "else": {"not": {"required": ["height"]}}}]
    add("suspension", "set_height", "调整空气悬架高度", {}, {"action": "raise"}, "把空气悬架升高一档", "raise/lower 表示升高或降低一档；set 时必须给出 low/normal/high 高度", schema=suspension_schema)
    add("suspension", "set_mode", "设置悬架模式", {"mode": enum(["comfort", "sport", "auto"], "舒适、运动、自动")}, {"mode": "comfort"}, "悬架切换到舒适模式")
    add("suspension", "set_loading_mode", "悬架装载模式", {"enabled": boolean}, {"enabled": True}, "开启悬架装载模式")
    add("sunroof", "set_position", "设置天窗开度", {"open_percent": number(0, 100)}, {"open_percent": 50}, "把天窗打开一半")
    add("sunroof", "set_tilt", "天窗翘起开关", {"enabled": boolean}, {"enabled": True}, "将天窗翘起")
    add("sunroof", "set_sunshade", "设置天窗遮阳帘", {"open_percent": number(0, 100)}, {"open_percent": 0}, "完全关闭天窗遮阳帘")
    add("sunroof", "stop", "停止天窗运动", {}, {}, "停止天窗当前运动")
    assert len(tools) == len(scenarios) == 50
    next(t for t in tools if t["id"] == "control.multimedia.select_track")["version"] = "1.0.1"
    # Playing a named track is a composition, not just selecting it.
    scenarios[28]["instruction"] = "播放示例曲目《模拟晴天》"
    scenarios[28]["expected"]["calls"].append({"tool_id": "control.multimedia.playback", "arguments": {"action": "play"}})

    data = {
        "equipment": {"climate": True, "windows": True, "seats": True, "multimedia": True, "lights": True, "mirrors": True, "air_suspension": True, "sunroof": True},
        "interfaces": {"suspension": True},
        "state": {
            "climate": {"driver_temperature_c": 22, "passenger_temperature_c": 22, "fan_speed": 2, "airflow": "face", "ac_enabled": True},
            "windows": {"front_left_open_percent": 0, "front_right_open_percent": 0, "rear_left_open_percent": 0, "rear_right_open_percent": 0},
            "seats": {"driver_heating_level": 0, "driver_ventilation_level": 0, "passenger_heating_level": 0},
            "multimedia": {"volume_percent": 20, "muted": False, "source": "bluetooth", "playing": False},
            "lights": {"ambient_color": "#FFFFFF", "brightness_percent": 30, "headlight_mode": "auto"},
            "mirrors": {"folded": False, "heating": False, "auto_fold": True},
            "suspension": {"available": True, "height": "normal", "mode": "comfort"},
            "sunroof": {"open_percent": 0, "tilted": False, "sunshade_open_percent": 0},
            "driving": {"is_parked": True, "speed_kmh": 0},
        },
        "sample_observed_at": "2026-11-14T00:00:00+08:00",
    }
    profiles = [{"id": "demo-ready", "title": "配置完整 / 驻车", "description": "所有状态均为固定示例，不代表实时车辆", "data": data}]
    for pid, title in [("demo-no-suspension", "未配备空气悬架"), ("demo-interface-missing", "悬架接口未开放"), ("demo-driving", "车辆行驶中")]:
        modified = deepcopy(data)
        if pid == "demo-no-suspension":
            modified["equipment"]["air_suspension"] = False
            modified["state"]["suspension"] = {"available": False, "height": None, "mode": None}
        elif pid == "demo-interface-missing":
            modified["interfaces"]["suspension"] = False
        else:
            modified["state"]["driving"] = {"is_parked": False, "speed_kmh": 45}
        profiles.append({"id": pid, "title": title, "description": "用于失败原因查证的模拟场景", "data": modified})

    query_scenarios = []
    for category, (label, description) in categories.items():
        content = f"模拟车型的{label}功能包括：{description}。参数范围以工具 Schema 为准，本说明不是实际车辆手册。"
        if category == "suspension":
            content += "空气悬架为选配；需要查询本车配置核实。模拟调节接口要求已配备、接口开放且处于驻车状态。"
        result = {"title": f"{label}使用说明（模拟）", "text": content, "source": {"kind": "mock_manual", "document": "demo-vehicle-manual-v1", "section": category}}
        manual = {"id": f"manual.{category}.lookup", "version": "1.0.0", "kind": "manual", "category_path": ["manual", category], "title": f"查询{label}车书", "description": f"查询模拟{label}使用说明与限制，不证明本车实际配备。", "aliases": [label, "说明书", "车书", category], "enabled": True, "input_schema": obj({"topic": enum(["overview"], "overview 表示功能概述、用法和限制")}), "output_schema": infer(result), "mock": {"mode": "argument_lookup", "argument": "topic", "cases": {"overview": result}}, "examples": [{"instruction": f"查一下车书里的{label}功能说明", "arguments": {"topic": "overview"}}]}
        tools.append(manual)
        state_schema = infer(data["state"][category])
        if category == "suspension":
            state_schema["properties"]["height"] = {"type": ["string", "null"]}
            state_schema["properties"]["mode"] = {"type": ["string", "null"]}
        status = {"id": f"status.{category}.get_state", "version": "1.0.0", "kind": "status", "category_path": ["status", category], "title": f"查询{label}状态", "description": f"读取当前模拟 profile 的{label}状态，不是真实采集。", "aliases": [label, "状态", category], "enabled": True, "input_schema": obj({}), "output_schema": state_schema, "mock": {"mode": "profile_pointer", "pointer": f"/state/{category}"}, "examples": [{"instruction": f"查询当前{label}状态", "arguments": {}}]}
        tools.append(status)
        for item in [manual, status]:
            query_scenarios.append({"id": f"Q{len(query_scenarios)+1:03}", "kind": "query", "instruction": item["examples"][0]["instruction"], "profile_id": "demo-ready", "expected": {"outcome": "answered", "calls": [{"tool_id": item["id"], "arguments": item["examples"][0]["arguments"]}], "required_domains": [item["kind"]]}})
    for action, title, pointer, value in [("get_configuration", "查询本车配置和接口", "", {"equipment": data["equipment"], "interfaces": data["interfaces"]}), ("get_driving_state", "查询驾驶状态", "/state/driving", data["state"]["driving"])]:
        if not pointer:
            # Profile keeps a dedicated view so a configuration query does not expose all state.
            for profile in profiles:
                profile["data"]["configuration"] = {"equipment": deepcopy(profile["data"]["equipment"]), "interfaces": deepcopy(profile["data"]["interfaces"])}
            pointer = "/configuration"
        item = {"id": f"status.vehicle.{action}", "version": "1.0.0", "kind": "status", "category_path": ["status", "vehicle"], "title": title, "description": title + "，数据来自模拟配置快照，不是真车。", "aliases": [title, "车辆", "配置", "vehicle"], "enabled": True, "input_schema": obj({}), "output_schema": infer(value), "mock": {"mode": "profile_pointer", "pointer": pointer}, "examples": [{"instruction": title, "arguments": {}}]}
        tools.append(item)
        query_scenarios.append({"id": f"Q{len(query_scenarios)+1:03}", "kind": "query", "instruction": title, "profile_id": "demo-ready", "expected": {"outcome": "answered", "calls": [{"tool_id": item["id"], "arguments": {}}], "required_domains": ["status"]}})
    cross = []
    for i, pid in enumerate(["demo-no-suspension", "demo-interface-missing", "demo-driving"], 1):
        cross.append({"id": f"X{i:03}", "kind": "cross_domain", "instruction": "帮我把空气悬架升高一档；如果不能生成，请查车辆配置或车况，并结合车书说明原因。", "profile_id": pid, "expected": {"outcome": "unsupported", "calls": [], "required_domains": ["control", "manual", "status"]}})
    cross.append({"id": "X004", "kind": "cross_domain", "instruction": "查一下车书里空调的说明，再把所有分区空调设为23度。", "profile_id": "demo-ready", "expected": {"outcome": "mixed", "calls": [{"tool_id": "control.climate.set_temperature", "arguments": {"zone": "all", "temperature_c": 23}}], "required_domains": ["manual", "control"]}})
    return {"format_version": FORMAT, "package": {"name": "vehicle-capability-demo", "version": "1.0.0", "description": "50个车控工具、8个车书工具、10个车况工具；全部模拟"}, "directories": dirs, "tools": tools, "profiles": profiles, "scenarios": scenarios+query_scenarios+cross}


def write(directory):
    if directory.exists():
        raise SystemExit("目标目录已存在；为保护配置，请指定新的 --output 目录")
    bundle = build()
    def save(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2)+"\n")
    save(directory/"manifest.json", {k: bundle[k] for k in ["format_version", "package"]})
    for d in bundle["directories"]:
        save(directory/"directories"/(".".join(d["path"])+".json"), d)
    for tool in bundle["tools"]:
        save(directory/"tools"/Path(*tool["category_path"])/(tool["id"].split(".")[-1]+".json"), tool)
    for p in bundle["profiles"]:
        save(directory/"profiles"/(p["id"]+".json"), p)
    for kind, filename in [("control", "control-50"), ("query", "queries-18"), ("cross_domain", "cross-domain-4")]:
        save(directory/"scenarios"/(filename+".json"), [s for s in bundle["scenarios"] if s["kind"] == kind])
    for name, schema in schema_defs().items():
        save(directory/"schemas"/(name+".schema.json"), schema)
    result_protocol = ROOT / "protocols/capability-result.schema.json"
    save(directory/"schemas/result.schema.json", json.loads(result_protocol.read_text()))
    save(directory/"exports"/"vehicle-capabilities.bundle.json", bundle)
    print(json.dumps({"control": 50, "manual": 8, "status": 10, "profiles": len(bundle["profiles"]), "scenarios": len(bundle["scenarios"])}, ensure_ascii=False))


if __name__ == "__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT/"capabilities")
    write(parser.parse_args().output)
