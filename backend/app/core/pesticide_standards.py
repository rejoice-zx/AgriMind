# -*- coding: utf-8 -*-
"""
AgriMind 禁限用农药名称预警。

原先内置的 8 种药剂"历史参考"安全间隔期/剂量表来源不明、无登记证与标签证据，已删除（第三阶段）。
安全间隔期一律以产品标签为准（《农药管理条例》第三十四条），由农户录入并经专家复核。
"""

from typing import List, Optional


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
    "乙酰甲胺磷", "丁硫克百威", "乐果",
    # 2024 年禁限用名录（福建省农业农村厅转载）列明禁止用于蔬菜：氯唑磷；
    # 灭线磷自 2024-09-01 起禁止销售和使用。名单仍需按农业农村部最新附件逐项复核。
    "氯唑磷", "灭线磷",
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


