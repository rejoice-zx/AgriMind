# -*- coding: utf-8 -*-
"""
AgriMind 未核验历史农药参考候选（不作为安全期裁决依据）

以下配方及数值来自历史示例，尚无逐产品登记证和标签证据，不能证明规范符合性。
仅按完整名称返回待核验候选；实际作业须核验产品标签、作物与用法。
"""

from typing import Dict, Any, List, Optional


PESTICIDE_PHI_STANDARDS: Dict[str, Dict[str, Any]] = {
    "精甲霜·锰锌": {
        "standard_name": "68% 精甲霜·锰锌 水分散粒剂",
        "active_ingredients": ["精甲霜灵 4%", "代森锰锌 64%"],
        "target_diseases": ["番茄晚疫病", "番茄早疫病", "绵疫病"],
        "safety_interval_days": 7,
        "max_applications_per_season": 3,
        "dosage_range": "100~120 g/亩",
        "dilution_ratio": "500~800 倍液",
        "caution": "治疗兼保护性杀菌剂。降雨前禁止施药；每季最多施用3次，严格遵守7天安全间隔期以防鲜果农残超标。"
    },
    "代森锰锌": {
        "standard_name": "80% 代森锰锌 可湿性粉剂",
        "active_ingredients": ["代森锰锌 80%"],
        "target_diseases": ["番茄早疫病", "晚疫病", "炭疽病", "叶霉病"],
        "safety_interval_days": 15,
        "max_applications_per_season": 3,
        "dosage_range": "130~160 g/亩",
        "dilution_ratio": "600~800 倍液",
        "caution": "保护性广谱杀菌剂，需在发病前或初见病斑时喷雾；安全间隔期较长(15天)，采收盛期禁止喷洒！"
    },
    "嘧菌酯": {
        "standard_name": "250克/升 嘧菌酯 悬浮剂",
        "active_ingredients": ["嘧菌酯 250g/L"],
        "target_diseases": ["番茄白粉病", "早疫病", "叶霉病", "炭疽病"],
        "safety_interval_days": 3,
        "max_applications_per_season": 2,
        "dosage_range": "40~60 ml/亩",
        "dilution_ratio": "1000~1500 倍液",
        "caution": "甲氧基丙烯酸酯类杀菌剂，极易产生抗药性，每季最多使用2次；安全间隔期短(3天)，适合果实膨大转色期防控。"
    },
    "腐霉利": {
        "standard_name": "50% 腐霉利 可湿性粉剂",
        "active_ingredients": ["腐霉利 (速克灵) 50%"],
        "target_diseases": ["番茄灰霉病", "菌核病"],
        "safety_interval_days": 7,
        "max_applications_per_season": 2,
        "dosage_range": "60~100 g/亩",
        "dilution_ratio": "1000~1500 倍液",
        "caution": "灰霉病专用内吸杀菌剂；花期严禁高浓度喷淋花蕊；严格遵守7天安全间隔期。"
    },
    "异菌脲": {
        "standard_name": "500克/升 异菌脲 悬浮剂",
        "active_ingredients": ["异菌脲 500g/L"],
        "target_diseases": ["番茄早疫病", "灰霉病", "叶霉病"],
        "safety_interval_days": 7,
        "max_applications_per_season": 3,
        "dosage_range": "50~80 ml/亩",
        "dilution_ratio": "1000~1200 倍液",
        "caution": "二甲酰亚胺类触杀性杀菌剂，耐雨水冲刷性中等，安全间隔期为7天。"
    },
    "百菌清": {
        "standard_name": "75% 百菌清 可湿性粉剂",
        "active_ingredients": ["百菌清 75%"],
        "target_diseases": ["番茄早疫病", "晚疫病", "灰霉病", "叶斑病"],
        "safety_interval_days": 7,
        "max_applications_per_season": 3,
        "dosage_range": "110~150 g/亩",
        "dilution_ratio": "500~600 倍液",
        "caution": "非内吸长效保护剂，需均匀覆盖叶面正反面；雨前不宜施用。"
    },
    "春雷霉素": {
        "standard_name": "2% 春雷霉素 水剂",
        "active_ingredients": ["春雷霉素 2%"],
        "target_diseases": ["番茄叶霉病", "细菌性斑疹病", "溃疡病"],
        "safety_interval_days": 3,
        "max_applications_per_season": 3,
        "dosage_range": "100~120 ml/亩",
        "dilution_ratio": "400~500 倍液",
        "caution": "农用生物抗生素，安全低残留，安全间隔期为3天，适合绿色无公害蔬菜生产。"
    },
    "氟啶胺": {
        "standard_name": "500克/升 氟啶胺 悬浮剂",
        "active_ingredients": ["氟啶胺 500g/L"],
        "target_diseases": ["番茄晚疫病", "灰霉病"],
        "safety_interval_days": 5,
        "max_applications_per_season": 2,
        "dosage_range": "30~40 ml/亩",
        "dilution_ratio": "1500~2000 倍液",
        "caution": "强效广谱杀菌剂，禁止与乳油类农药及有机硅助剂混用以防产生药害。"
    }
}


def lookup_pesticide_standard(query_name: str) -> Optional[Dict[str, Any]]:
    """Exact full-name candidate lookup only; never registration or PHI approval."""
    if not query_name:
        return None
    q = "".join(query_name.split()).lower()
    for val in PESTICIDE_PHI_STANDARDS.values():
        if "".join(val["standard_name"].split()).lower() == q:
            return {**val, "verification_status": "unverified", "notice": "历史参考候选，须另核验登记证、产品标签、作物与用法"}
    return None


# 蔬菜/茄果类作物法定禁限用高毒农药负面清单 (全量依据农业农村部公告第 199/274/2032/2567 号、NY/T 393-2020 及《农药管理条例》)
BANNED_PESTICIDES_CATALOG: List[str] = [
    # 1. 农业农村部全面禁止生产销售使用的高毒农药 (20 种)
    "六六六", "滴滴涕", "毒杀芬", "甲胺磷", "对硫磷", "甲基对硫磷", "久效磷",
    "磷胺", "苯线磷", "地虫硫磷", "甲基硫环磷", "蝇毒磷", "治螟磷", "特丁硫磷",
    "三氯杀螨醇", "氟虫腈", "杀扑磷", "百草枯", "除草醚", "五氯酚钠",
    # 2. 农业农村部明确禁止在蔬菜/果树等作物上使用的高毒/限用农药 (13 种)
    "甲拌磷", "甲基异柳磷", "克百威", "呋喃丹", "水胺硫磷", "氧乐果", "灭多威",
    "涕灭威", "神农丹", "克线磷", "内吸磷", "硫环磷", "毒死蜱", "三唑磷",
    # 农业部公告第2552号：2019-08-01起禁止用于蔬菜等作物，含复配制剂。
    "乙酰甲胺磷", "丁硫克百威", "乐果"
]

# 相近名称子串保护，不代表登记、标签或适用范围已核验
SAFE_CHEMICAL_WHITELIST: List[str] = [
    "丙硫克百威",
    "杀虫双",
    "双甲脒",
]


def _mask_safe_chemicals(text: str) -> str:
    """对文本中的合法白名单农药进行无害化遮蔽，防止子串扫描误伤"""
    masked = text
    for i, safe_name in enumerate(SAFE_CHEMICAL_WHITELIST):
        if safe_name in masked:
            masked = masked.replace(safe_name, f"__SAFE_CHEM_{i}__")
    return masked


def find_all_banned_pesticides(text: str) -> List[str]:
    """
    扫描文本中包含的所有法定禁用/高毒农药化学品名称列表。
    采用白名单占位遮蔽保护机制，并遵循长词优先（Max Match）原则，
    本函数是番茄/蔬菜应用的名称预警，不代表其他作物适用性或完整登记标签核验。
    采用长词优先，避免把乙酰甲胺磷错报为甲胺磷。
    """
    if not text:
        return []

    # 1. 先遮蔽合法化学品白名单
    working_text = _mask_safe_chemicals(text.strip().lower())

    # 2. 按化学品名称长度降序排列（长词优先匹配），防止较短子串抢占较长名称
    sorted_catalog = sorted(BANNED_PESTICIDES_CATALOG, key=lambda x: len(x), reverse=True)

    found = []
    for banned in sorted_catalog:
        b_lower = banned.lower()
        if b_lower in working_text:
            found.append(banned)
            # 遮蔽已匹配的较长违禁词，防止包含它的较短词重复匹配同一词位置
            working_text = working_text.replace(b_lower, f"__BANNED_{len(found)}__")

    # 3. 按照在原始文本中出现的位置先后排序，保证首位返回与语义自然顺序一致
    found.sort(key=lambda chem: text.lower().find(chem.lower()))
    return found


def check_banned_pesticide(text: str) -> Optional[str]:
    """
    检查输入文本或模型推荐内容是否包含法定禁用/高毒农药化学品名称。
    若包含，返回首个匹配到的禁用农药名称；否则返回 None。
    逻辑与 find_all_banned_pesticides 完全自洽统一。
    """
    found = find_all_banned_pesticides(text)
    return found[0] if found else None


