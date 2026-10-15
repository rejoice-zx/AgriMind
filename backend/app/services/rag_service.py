# -*- coding: utf-8 -*-
"""
RAG服务模块 - 检索增强生成服务

功能:
- 知识检索和上下文构建
- 查询改写
- 多路向量检索 + 关键词覆盖度 bonus
- 倒排索引加速关键词检索
- 置信度计算
"""

import logging
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional, Set, Tuple, Any
from app.core.knowledge_contract import METADATA_KEYS, metadata_matches, candidate_eligible
from app.core.weather_policy import agriculture_today

logger = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=4)


import math


@dataclass
class RAGSearchResult:
    """RAG搜索结果（支持电商与农业混合场景）"""
    query: str = ""
    rewritten_query: str = ""
    retrieved_items: List[Dict[str, Any]] = field(default_factory=list)
    context_text: str = ""
    confidence: float = 0.0
    search_method: str = "vector"  # "vector", "keyword", "hybrid"
    final_prompt: str = ""
    retrieval_path: str = "none"
    intent: str = "GENERAL"  # FACT, DIAGNOSIS, DECISION, PLANNING, GENERAL
    extracted_entities: Dict[str, Any] = field(default_factory=dict)
    need_expert: bool = False
    # 正式字段（DS-09）：低置信度 / 无匹配规程等需人工介入的理由，必须随 to_dict()
    # 贯穿到 HTTP/SSE/Agent；此前只在 service 内被赋成动态属性，序列化时丢失。
    expert_reason: Optional[str] = None
    filter_metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "query": self.query,
            "rewritten_query": self.rewritten_query,
            "retrieved_items": self.retrieved_items,
            "context_text": self.context_text,
            "confidence": self.confidence,
            "search_method": self.search_method,
            "final_prompt": self.final_prompt,
            "retrieval_path": self.retrieval_path,
            "intent": self.intent,
            "extracted_entities": self.extracted_entities,
            "need_expert": self.need_expert,
            "expert_reason": self.expert_reason,
            "filter_metadata": self.filter_metadata,
        }


# 农业种植与农事实操停用短语
STOP_PHRASES = [
    "请问一下", "想问一下", "问一下", "想知道", "我想问", "我想知道",
    "可不可以", "能不能", "怎么样", "好不好", "该怎么办",
    "帮我看看", "帮我查查", "帮我问问", "看下", "瞧瞧",
    "麻烦问一下", "麻烦帮我", "请教一下", "农技专家",
    "你好", "您好", "请问", "我想", "帮我", "麻烦",
    "谢谢", "感谢", "好的", "可以吗", "行吗", "好吗",
]

# 农业专业领域同义词与术语对齐映射
SYNONYM_MAP = {
    # 作物命名
    "西红柿": "番茄", "洋柿子": "番茄",
    "黄瓜": "黄瓜", "青瓜": "黄瓜", "胡瓜": "黄瓜",
    "彩椒": "甜椒", "菜椒": "甜椒", "青椒": "辣椒",
    # 农事作业
    "打药": "喷雾施药", "喷药": "喷雾施药", "用药": "防治药剂", "喷施": "叶面喷施",
    "浇水": "水肥滴灌", "给水": "灌溉", "大水漫灌": "灌溉",
    "上肥": "追施肥料", "追肥": "追施水溶肥", "冲肥": "随水冲施", "追肥": "根外追肥",
    "打杈": "整枝打杈", "抹芽": "打杈整枝", "掐尖": "打顶摘心", "落蔓": "植株落蔓",
    "摘果": "批次采收", "收菜": "采收",
    # 生长阶段
    "苗子": "苗期", "幼苗": "苗期", "定植": "定植移栽", "缓苗": "定植缓苗期",
    "开花": "开花坐果期", "坐果": "开花坐果期", "授粉": "开花坐果期",
    "长果子": "果实膨大期", "膨果": "果实膨大期", "转色": "果实转色成熟期",
    # 病斑与表型
    "生病": "病害侵染", "得病": "发病", "生烂斑": "坏死病斑", "长毛": "霉层病斑",
    "黄叶": "叶片黄化褪绿", "干枯": "叶缘枯焦", "死棵": "根腐立枯茎基腐",
    "烂果": "果实软腐", "水浸状": "水浸状暗绿斑", "黑斑": "同心轮纹病斑",
    # 环境气象
    "太潮": "高湿密闭环境", "不通风": "通风不良", "闷热": "高温高湿",
    "下雨": "降雨天气", "连阴天": "寡照阴雨天气", "倒春寒": "低温冷害",
}


# ==================== 工业级 BM25 算法实现 ====================

class BM25Okapi:
    """标准 BM25Okapi 关键词打分算法实现"""
    def __init__(self, corpus_tokens: List[List[str]], k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.corpus_size = len(corpus_tokens)
        self.avgdl = sum(len(x) for x in corpus_tokens) / max(1, self.corpus_size)
        self.doc_freqs: List[Dict[str, int]] = []
        self.idf: Dict[str, float] = {}
        self.doc_lens = [len(x) for x in corpus_tokens]

        nd: Dict[str, int] = defaultdict(int)
        for doc in corpus_tokens:
            frequencies = defaultdict(int)
            for word in doc:
                frequencies[word] += 1
            self.doc_freqs.append(frequencies)
            for word in frequencies.keys():
                nd[word] += 1

        for word, freq in nd.items():
            # 经典平滑 IDF 公式
            self.idf[word] = math.log((self.corpus_size - freq + 0.5) / (freq + 0.5) + 1.0)

    def get_scores(self, query_tokens: List[str]) -> List[float]:
        """计算查询与语料中所有文档的 BM25 匹配得分"""
        scores = [0.0] * self.corpus_size
        for word in query_tokens:
            if word not in self.idf:
                continue
            q_idf = self.idf[word]
            for doc_idx in range(self.corpus_size):
                freq = self.doc_freqs[doc_idx].get(word, 0)
                if freq > 0:
                    numerator = freq * (self.k1 + 1)
                    denominator = freq + self.k1 * (1 - self.b + self.b * (self.doc_lens[doc_idx] / max(1.0, self.avgdl)))
                    scores[doc_idx] += q_idf * (numerator / denominator)
        return scores


# ==================== Query Understanding 实体抽取与路由 ====================

class QueryUnderstanding:
    """查询理解：农业实体抽取与 Query Router 意图分流"""
    CROPS = ["番茄", "西红柿", "玉米", "水稻", "小麦", "黄瓜", "辣椒"]
    GROWTH_STAGES = ["苗期", "育苗期", "定植期", "缓苗期", "开花期", "坐果期", "膨果期", "果实膨大期", "成熟期", "转色期", "采收期"]
    DISEASES = [
        "叶霉病", "早疫病", "晚疫病", "灰霉病", "病毒病", "青枯病", "溃疡病",
        "脐腐病", "白粉病", "细菌性髓部坏死", "猝倒病", "立枯病", "筋腐病",
        "炭疽病", "根结线虫病", "根结线虫", "叶螨", "红蜘蛛"
    ]
    TOPICS = {
        "disease": ["病害", "生病", "枯死", "斑点", "霉层", "烂果", "萎蔫", "发病", "防治"],
        "pest": ["虫害", "蚜虫", "白粉虱", "红蜘蛛", "害虫", "钻蛀", "潜叶蛾"],
        "fertilizer": ["施肥", "追肥", "底肥", "水溶肥", "复合肥", "氮肥", "磷肥", "钾肥", "微肥", "高钾"],
        "irrigation": ["灌溉", "浇水", "滴灌", "排水", "积水", "缺水", "干旱"],
        "cultivation": ["整枝", "打杈", "疏花", "授粉", "吊蔓", "修剪", "定植", "育苗"],
        "weather": ["天气", "降雨", "下雨", "湿度", "温度", "低温", "高温", "霜冻"]
    }
    REGIONS = ["江苏", "南京", "苏州", "山东", "河北", "河南", "北方", "南方"]

    @classmethod
    def extract_entities(cls, query: str) -> Dict[str, Any]:
        """从用户查询文本中抽取农业核心命名实体"""
        entities: Dict[str, Any] = {}
        for c in cls.CROPS:
            if c in query:
                entities["crop"] = "番茄" if c in ["番茄", "西红柿"] else c
                break
        for s in cls.GROWTH_STAGES:
            if s in query:
                entities["growth_stage"] = s
                break
        for d in cls.DISEASES:
            if d in query:
                entities["disease"] = d
                break
        for r in cls.REGIONS:
            if r in query:
                entities["region"] = r
                break
        for topic_key, keywords in cls.TOPICS.items():
            if any(k in query for k in keywords):
                entities["topic"] = topic_key
                break
        return entities

    @classmethod
    def route_intent(cls, query: str, entities: Dict[str, Any]) -> str:
        """Query Router：将用户问题精准分流至五大意图类型"""
        from app.core.intent_rules import classify_intent
        return classify_intent(query, entities)



class RAGService:
    """RAG服务类 - 检索增强生成"""

    def __init__(
        self,
        similarity_threshold: float = 0.4,
        retrieval_top_k: int = 5,
        context_max_chars: int = 4000,
        context_top_n: int = 3,
        use_rewrite: bool = True,
        source_labels: bool = False,
    ):
        self.similarity_threshold = similarity_threshold
        self.retrieval_top_k = retrieval_top_k
        self.context_max_chars = context_max_chars
        self.context_top_n = context_top_n
        self.use_rewrite = use_rewrite
        self.source_labels = source_labels
        self._vector_service = None
        self._last_result: Optional[RAGSearchResult] = None

    def _get_vector_service(self):
        if self._vector_service is None:
            from app.services.vector_service import VectorService
            self._vector_service = VectorService()
        return self._vector_service

    # ==================== 查询改写 ====================

    def rewrite_query(self, query: str) -> str:
        """查询改写 - 停用词过滤 + 同义词扩展"""
        cleaned_query = query

        for phrase in STOP_PHRASES:
            cleaned_query = cleaned_query.replace(phrase, " ")

        cleaned_query = " ".join(cleaned_query.split()).strip()

        if len(cleaned_query) < 2:
            cleaned_query = query

        expanded_terms = []
        for user_term, search_term in SYNONYM_MAP.items():
            if user_term in query:
                if search_term not in cleaned_query and search_term not in expanded_terms:
                    expanded_terms.append(search_term)

        if expanded_terms:
            return f"{cleaned_query} {' '.join(expanded_terms)}"
        return cleaned_query

    # ==================== Token 提取 ====================

    def _extract_tokens(self, text: str) -> List[str]:
        """提取文本中的 token（英文词 + 中文 bigram）"""
        s = (text or "").strip()
        if not s:
            return []

        tokens: List[str] = []
        s_lower = s.lower()
        tokens.extend(re.findall(r"[a-z0-9]{2,}", s_lower))

        for seg in re.findall(r"[\u4e00-\u9fff]{2,}", s):
            seg = seg.strip()
            if not seg:
                continue
            tokens.append(seg)
            for i in range(min(len(seg) - 1, 12)):
                tokens.append(seg[i:i + 2])

        seen: Set[str] = set()
        uniq: List[str] = []
        for t in tokens:
            if t and t not in seen:
                seen.add(t)
                uniq.append(t)
            if len(uniq) >= 40:
                break
        return uniq

    # ==================== 倒排索引 ====================

    def _build_inverted_index(
        self, knowledge_items: List[Dict[str, Any]]
    ) -> Dict[str, Set[int]]:
        """构建 token → 条目索引位置 的倒排索引，用于关键词检索粗筛。"""
        inverted: Dict[str, Set[int]] = defaultdict(set)
        for idx, item in enumerate(knowledge_items):
            item_text = f"{item.get('question', '')} {item.get('answer', '')}"
            for token in self._extract_tokens(item_text):
                inverted[token].add(idx)
            for kw in item.get("keywords", []):
                if kw:
                    inverted[kw.lower()].add(idx)
        return inverted

    # ==================== 置信度 ====================

    def _compute_confidence(
        self,
        query: str,
        results: List[Tuple[Dict, float]]
    ) -> float:
        if not results:
            return 0.0

        top1 = results[0][1]
        top2 = results[1][1] if len(results) >= 2 else None
        gap = (top1 - top2) if top2 is not None else 0.0

        item = results[0][0]
        keywords = item.get("keywords", [])
        if keywords:
            denom = min(len(keywords), 6)
            hit = sum(1 for kw in keywords[:denom] if kw in query)
            keyword_cover = hit / max(1, denom)
        else:
            keyword_cover = 0.0

        top_k_bonus = (min(len(results), 5) - 1) / 4 if len(results) >= 2 else 0.0

        confidence = top1
        confidence += 0.15 * max(0.0, min(gap, 1.0))
        confidence += 0.08 * max(0.0, min(keyword_cover, 1.0))
        confidence += 0.04 * max(0.0, min(top_k_bonus, 1.0))

        return max(0.0, min(1.0, confidence))

    # ==================== 上下文构建 ====================

    def _build_context(
        self,
        results: List[Tuple[Dict, float]],
        chunk_map: Optional[Dict[str, str]] = None,
    ) -> str:
        """构建上下文文本。

        如果 chunk_map 中有该知识 ID 对应的命中 chunk 片段，
        优先使用 chunk 片段（更精准）；否则回退到整条 question/answer。
        """
        if not results:
            return ""

        context_parts: List[str] = []
        total = 0

        for item, _ in results[:self.context_top_n]:
            item_id = item.get("id", "")
            chunk_snippet = chunk_map.get(item_id) if chunk_map else None

            if chunk_snippet:
                question = item.get("question", "")
                part = f"问题：{question}\n内容：{chunk_snippet}"
            else:
                question = item.get("question", "")
                answer = item.get("answer", "")
                part = f"问题：{question}\n答案：{answer}"

            if self.source_labels:
                part = f"知识ID：{item_id} | 来源标注：{item.get('source_name') or '未填写'} | 分级标注：{item.get('authority_level') or '未标注'}（未自动核验）\n{part}"
            next_total = total + len(part) + (7 if context_parts else 0)
            if self.context_max_chars > 0 and next_total > self.context_max_chars:
                if not context_parts:
                    context_parts.append(part[:self.context_max_chars])
                break

            context_parts.append(part)
            total = next_total

        return "\n\n---\n\n".join(context_parts)

    # ==================== 关键词覆盖度 bonus ====================

    def _keyword_coverage_bonus(
        self,
        query: str,
        item: Dict[str, Any],
    ) -> float:
        """计算关键词覆盖度 bonus，用于向量检索结果的二次排序。

        bonus = 0.2 * (命中关键词数 / min(关键词总数, 3))，上限 0.2。
        """
        keywords = item.get("keywords", [])
        if not keywords:
            return 0.0
        denom = min(len(keywords), 3)
        hits = sum(1 for kw in keywords[:denom] if kw and kw in query)
        return 0.2 * min(hits, denom) / max(1, denom)

    # ==================== 搜索主入口 ====================

    async def search(
        self,
        query: str,
        knowledge_items: List[Dict[str, Any]],
        threshold: float = None
    ) -> RAGSearchResult:
        threshold = self.similarity_threshold if threshold is None else threshold

        result = RAGSearchResult()
        result.query = query
        rewritten = self.rewrite_query(query) if self.use_rewrite else query
        result.rewritten_query = rewritten

        # 多路查询：同时用 rewritten_query 和原始 query 做向量检索
        queries = [rewritten]
        if rewritten != query:
            queries.append(query)

        vector_results, chunk_map = await self._vector_search_multi(
            queries, knowledge_items, threshold
        )

        if vector_results:
            result.search_method = "vector"
            results = vector_results
        else:
            result.search_method = "keyword"
            results = self._keyword_search(
                rewritten, knowledge_items, threshold
            )
            chunk_map = {}

        result.retrieved_items = [
            {"id": item.get("id"), "question": item.get("question"),
             "answer": item.get("answer", ""), "score": score,
             "category": item.get("category", "通用")}
            for item, score in results
        ]

        result.context_text = self._build_context(results, chunk_map)
        result.confidence = self._compute_confidence(query, results)

        self._last_result = result
        return result

    # ==================== BM25 稀疏检索 ====================

    def _bm25_search(
        self,
        query: str,
        knowledge_items: List[Dict[str, Any]]
    ) -> Dict[str, float]:
        """计算候选知识条目与查询的归一化 BM25 得分"""
        if not knowledge_items:
            return {}

        query_tokens = self._extract_tokens(query)
        if not query_tokens:
            return {}

        # 对所有条目提取 token 构建语料
        corpus_tokens = [
            self._extract_tokens(f"{it.get('question', '')} {it.get('answer', '')} {' '.join(it.get('keywords', []))}")
            for it in knowledge_items
        ]

        bm25 = BM25Okapi(corpus_tokens)
        raw_scores = bm25.get_scores(query_tokens)

        # 基于查询中所有 token 的有效 IDF 和计算理论满分基准，防止仅命中 1 个词被虚假归一化为 1.0
        query_idf_sum = sum(max(0.0, bm25.idf.get(t, 0.0)) for t in query_tokens)
        max_theoretical = max(1.0, (bm25.k1 + 1.0) * query_idf_sum)

        scores_map: Dict[str, float] = {}

        for idx, it in enumerate(knowledge_items):
            kid = it.get("id")
            if not kid:
                continue
            # 严格依据理论满分上限进行打分归一化
            norm_score = min(1.0, max(0.0, raw_scores[idx] / max_theoretical))
            scores_map[kid] = norm_score

        return scores_map

    # ==================== Hybrid RAG 混合检索主入口 ====================

    async def hybrid_search(
        self,
        query: str,
        knowledge_items: List[Dict[str, Any]],
        filter_metadata: Optional[Dict[str, Any]] = None,
        threshold: float = 0.30,
        top_k: int = 5,
        strict_threshold: bool = False,
        eligibility_date: Optional[date] = None,
    ) -> RAGSearchResult:
        """
        基于实体抽取、意图路由、元数据前置过滤、向量检索与 BM25 加权融合的 Hybrid RAG
        """
        result = RAGSearchResult()
        result.query = query
        rewritten = self.rewrite_query(query) if self.use_rewrite else query
        result.search_method = "hybrid"
        result.rewritten_query = rewritten

        # 统一转为 dict 格式以兼容 SQLAlchemy ORM 对象与字典
        standard_items = []
        for it in knowledge_items:
            if isinstance(it, dict):
                standard_items.append(it)
            elif hasattr(it, "to_dict"):
                standard_items.append(it.to_dict())
            else:
                standard_items.append({
                    "id": getattr(it, "id", None),
                    "question": getattr(it, "question", ""),
                    "answer": getattr(it, "answer", ""),
                    "keywords": getattr(it, "keywords", []),
                    "category": getattr(it, "category", ""),
                    "crop": getattr(it, "crop", None),
                    "region": getattr(it, "region", None),
                    "growth_stage": getattr(it, "growth_stage", None),
                    "topic": getattr(it, "topic", None),
                    "disease": getattr(it, "disease", None),
                    "source_type": getattr(it, "source_type", "unverified"),
                    "source_name": getattr(it, "source_name", ""),
                    "authority_level": getattr(it, "authority_level", None),
                    "version": getattr(it, "version", None),
                    "is_current": getattr(it, "is_current", None),
                    "effective_from": getattr(it, "effective_from", None),
                    "effective_to": getattr(it, "effective_to", None),
                })
        eligibility_date = eligibility_date or agriculture_today()
        knowledge_items = [item for item in standard_items if candidate_eligible(item, eligibility_date)]


        # 1. 实体抽取与意图分流
        entities = QueryUnderstanding.extract_entities(query)
        intent = QueryUnderstanding.route_intent(query, entities)
        result.intent = intent
        result.extracted_entities = entities

        # 2. 合并前置元数据过滤参数（严格基于调用方指定过滤或强作物实体，杜绝跨作物错误）
        combined_filter = dict(filter_metadata or {})
        if "crop" not in combined_filter and "crop" in entities:
            combined_filter["crop"] = entities["crop"]
        result.filter_metadata = combined_filter

        # 3. 候选集元数据过滤（前置硬过滤：严格锁定作物归属，防止跨作物错配）
        target_items = knowledge_items
        if combined_filter:
            filtered = []
            for item in knowledge_items:
                match = True
                for fk, fv in combined_filter.items():
                    if not fv:
                        continue
                    iv = item.get(fk)
                    if fk in METADATA_KEYS:
                        matched = metadata_matches(item, fk, fv, combined_filter.get('crop'))
                    else:
                        matched = bool(iv) and str(iv).strip().lower() == str(fv).strip().lower()
                    if not matched:
                        match = False
                        break
                if match:
                    filtered.append(item)
            # 严格前置过滤：若指定了条件但无匹配条目，严禁平滑回退至全库，防止跨作物误诊
            target_items = filtered
            if not target_items:
                result.retrieved_items = []
                result.need_expert = True
                # DS-53：字段名是 confidence（to_dict 读的就是它）；写 confidence_score
                # 只会产生一个没人读的动态属性，低置信信号会静默丢失。
                result.confidence = 0.0
                crop_desc = combined_filter.get("crop") or "该属性"
                result.expert_reason = f"知识库暂无满足【{crop_desc}】过滤条件的可用记录，建议人工复核；检索本身未创建专家工单"
                return result

        if not target_items:
            result.need_expert = True
            result.expert_reason = '暂无满足来源、版本及有效期条件的候选记录，请补充资料后人工复核；本次未调用检索向量服务'
            return result

        # 4. 多路向量检索 (Dense)
        queries = [rewritten]
        if rewritten != query:
            queries.append(query)

        vector_results, chunk_map = await self._vector_search_multi(
            queries, target_items, threshold=threshold
        )
        vector_score_map = {item.get("id"): score for item, score in vector_results}

        # 5. BM25 检索 (Sparse)
        bm25_score_map = self._bm25_search(rewritten, target_items)

        # 6. 双路融合打分 (Hybrid Fusion & Rerank)
        item_map = {it.get("id"): it for it in target_items if it.get("id")}
        all_candidate_ids = set(vector_score_map.keys()).union(
            [kid for kid, s in bm25_score_map.items() if s > 0.0]
        )
        if not all_candidate_ids:
            all_candidate_ids = set(item_map.keys())

        # 权威等级加权权重表 (L1 权威规范 > L2 专业文献 > L3 农技推广 > L4 专家案例)
        authority_bonus_map = {
            "L1": 0.08,
            "L2": 0.05,
            "L3": 0.02,
            "L4": 0.01,
        }

        hybrid_scored: List[Tuple[Dict[str, Any], float, float, float]] = []
        has_vector_service = bool(vector_score_map)
        result.retrieval_path = "hybrid" if has_vector_service else "bm25"

        for kid in all_candidate_ids:
            item = item_map.get(kid)
            if not item:
                continue

            v_score = vector_score_map.get(kid, 0.0)
            b_score = bm25_score_map.get(kid, 0.0)

            # 稠密与稀疏加权：有向量服务时 0.55+0.35；离线/无向量服务时 BM25 主导
            if has_vector_service:
                base_score = 0.55 * v_score + 0.35 * b_score
            else:
                base_score = 0.80 * b_score

            # 关键词覆盖度加成
            kw_bonus = self._keyword_coverage_bonus(query, item)
            # 权威等级加成
            auth_level = item.get("authority_level")
            auth_bonus = authority_bonus_map.get(auth_level, 0.0)

            # 元数据精准契合加成（物候期、病害实体匹配加成）
            meta_bonus = 0.0
            target_stage = combined_filter.get("growth_stage") or entities.get("growth_stage")
            if target_stage and metadata_matches(item, 'growth_stage', target_stage, combined_filter.get('crop')):
                meta_bonus += 0.06
            target_disease = combined_filter.get("disease") or entities.get("disease")
            if target_disease and metadata_matches(item, 'disease', target_disease, combined_filter.get('crop')):
                meta_bonus += 0.08

            final_score = min(1.0, max(0.0, base_score + kw_bonus + auth_bonus + meta_bonus))

            effective_threshold = threshold if has_vector_service else min(threshold, 0.15)
            accepted = final_score >= threshold if strict_threshold else (final_score >= effective_threshold or v_score >= threshold or b_score >= 0.15)
            if accepted:
                hybrid_scored.append((item, final_score, v_score, b_score))

        # 按融合最终得分降序
        hybrid_scored.sort(key=lambda x: x[1], reverse=True)
        top_results = hybrid_scored[:top_k]

        # 7. 格式化输出与置信度计算
        result.retrieved_items = [
            {
                "id": item.get("id"),
                "question": item.get("question"),
                "answer": item.get("answer", ""),
                "score": round(score, 4),
                "vector_score": round(v_score, 4),
                "bm25_score": round(b_score, 4),
                "authority_level": item.get("authority_level") or "未核验",
                "source_name": item.get("source_name"),
                "crop": item.get("crop"),
                "growth_stage": item.get("growth_stage"),
                "disease": item.get("disease"),
                "category": item.get("category", "通用"),
                "source_type": item.get("source_type"), "version": item.get("version"),
                "publish_date": item.get("publish_date"), "url": item.get("url"),
                "effective_from": item.get("effective_from"), "effective_to": item.get("effective_to"),
                "source_verified": False
            }
            for item, score, v_score, b_score in top_results
        ]

        tuple_results = [(item, score) for item, score, _, _ in top_results]
        result.context_text = self._build_context(tuple_results, chunk_map)
        result.confidence = round(self._compute_confidence(query, tuple_results), 4)

        # 8. 结合方案书第二十九节：置信度低于阈值标记触发专家复核
        from app.core.confidence_policy import CONFIDENCE_EXPERT_GATE
        result.need_expert = result.confidence < CONFIDENCE_EXPERT_GATE

        self._last_result = result
        return result

    # ==================== 多路向量检索 ====================

    async def _vector_search_multi(
        self,
        queries: List[str],
        knowledge_items: List[Dict[str, Any]],
        threshold: float,
    ) -> Tuple[List[Tuple[Dict, float]], Dict[str, str]]:
        """多路向量检索：对多个 query 分别 embedding + 搜索，
        chunk 级命中聚合回知识条目，加关键词覆盖度 bonus。

        Returns:
            (results, chunk_map)
            - results: [(item_dict, final_score), ...]
            - chunk_map: {knowledge_id: best_chunk_text} 用于上下文构建
        """
        from app.services.performance_service import get_performance_service
        from app.services.knowledge_service import split_chunk_id
        import time as _time
        perf = get_performance_service()
        start = _time.perf_counter()
        success = True
        try:
            vector_service = self._get_vector_service()

            if vector_service.contract is not None and not vector_service.compatibility()[0]:
                return [], {}
            if not vector_service.is_embedding_available():
                logger.warning("Embedding服务不可用")
                return [], {}

            candidate_k = self.retrieval_top_k * 3
            item_map = {item.get("id"): item for item in knowledge_items}

            # 聚合：knowledge_id → max_score
            best_scores: Dict[str, float] = {}
            # 记录最佳 chunk 的原始 ID（用于获取 chunk 文本）
            best_chunk_ids: Dict[str, str] = {}

            # 批量 embedding 所有 query
            embeddings = await vector_service.embed_texts(queries)
            if not embeddings:
                return [], {}

            for q_idx, emb in enumerate(embeddings):
                if emb is None:
                    continue
                search_results = await vector_service.search(emb, top_k=candidate_k)
                for raw_id, score in search_results:
                    if score < threshold:
                        continue
                    knowledge_id, _ = split_chunk_id(raw_id)
                    if knowledge_id not in best_scores or score > best_scores[knowledge_id]:
                        best_scores[knowledge_id] = score
                        best_chunk_ids[knowledge_id] = raw_id

            if not best_scores:
                return [], {}

            # 加关键词覆盖度 bonus
            original_query = queries[0]
            scored: List[Tuple[str, float]] = []
            for kid, max_score in best_scores.items():
                item = item_map.get(kid)
                if not item:
                    continue
                bonus = self._keyword_coverage_bonus(original_query, item)
                final_score = min(1.0, max_score + bonus)
                scored.append((kid, final_score))

            scored.sort(key=lambda x: x[1], reverse=True)

            # 构建 chunk_map：knowledge_id → chunk 文本片段
            # 从向量索引的 ID 映射中获取 chunk 文本（这里用知识条目的 answer 截取）
            chunk_map: Dict[str, str] = {}
            results: List[Tuple[Dict, float]] = []
            for kid, score in scored:
                item = item_map.get(kid)
                if not item:
                    continue
                results.append((item, score))
                # 如果命中的是 chunk（含 #chunk_ 分隔符），记录到 chunk_map
                raw_id = best_chunk_ids.get(kid, kid)
                _, chunk_idx = split_chunk_id(raw_id)
                if chunk_idx >= 0:
                    # 重新切分文本获取对应 chunk 片段
                    from app.services.knowledge_service import chunk_text as do_chunk
                    from app.core.config import Config
                    cfg = Config()
                    cs = int(cfg.get("chunk_size", 500))
                    co = int(cfg.get("chunk_overlap", 50))
                    full_text = f"{item.get('question', '')}\n{item.get('answer', '')}"
                    chunks = do_chunk(full_text, cs, co)
                    if chunk_idx < len(chunks):
                        chunk_map[kid] = chunks[chunk_idx]

                if len(results) >= self.retrieval_top_k:
                    break

            return results, chunk_map

        except Exception:
            success = False
            logger.exception("向量检索失败")
            return [], {}
        finally:
            duration = _time.perf_counter() - start
            perf.record("vector_search", duration, success)

    # ==================== 关键词检索（倒排索引） ====================

    def _keyword_search(
        self,
        query: str,
        knowledge_items: List[Dict[str, Any]],
        threshold: float
    ) -> List[Tuple[Dict, float]]:
        """关键词检索 — 先用倒排索引粗筛候选，再对候选算分。"""
        from app.services.performance_service import get_performance_service
        import time as _time
        perf = get_performance_service()
        start = _time.perf_counter()
        success = True
        try:
            query_tokens = set(self._extract_tokens(query))
            if not query_tokens:
                return []

            # 构建倒排索引
            inverted = self._build_inverted_index(knowledge_items)

            # 粗筛：只保留至少有一个 token 命中的候选
            candidate_indices: Set[int] = set()
            for token in query_tokens:
                if token in inverted:
                    candidate_indices.update(inverted[token])

            if not candidate_indices:
                return []

            results = []

            for idx in candidate_indices:
                item = knowledge_items[idx]
                item_text = f"{item.get('question', '')} {item.get('answer', '')}"
                item_tokens = set(self._extract_tokens(item_text))

                for kw in item.get("keywords", []):
                    if kw:
                        item_tokens.add(kw.lower())

                if not item_tokens:
                    continue

                intersection = query_tokens & item_tokens
                if not intersection:
                    continue

                # Jaccard 相似度
                score = len(intersection) / len(query_tokens | item_tokens)

                # 关键词命中加分
                keywords = item.get("keywords", [])
                kw_hits = sum(1 for kw in keywords if kw and kw.lower() in query.lower())
                if kw_hits > 0:
                    score += 0.2 * min(kw_hits, 3) / 3

                score = min(1.0, score)

                if score >= threshold:
                    results.append((item, score))

            results.sort(key=lambda x: x[1], reverse=True)
            return results[:self.retrieval_top_k]
        except Exception:
            success = False
            raise
        finally:
            duration = _time.perf_counter() - start
            perf.record("keyword_search", duration, success)

    # ==================== Prompt 构建 ====================

    def build_system_prompt(self, context_text: Optional[str] = None) -> str:
        base_prompt = (
            "你是由数字农业研究团队与国家植保专家联合构建的 AgriMind 农业智能决策与技术规程咨询助理。"
            "你的核心使命是为现代种植农户、农技推广人员与示范农场提供严谨、科学、合规且可追溯的植保决策与农事实操指导。"
            "在解答问题时，请务必严格遵循国家标准（GB/T）与农药登记安全准则，严防农药残留超标与药害漂移事故；"
            "所有用药与水肥处方均需说明适期、施用方法、安全间隔期（PHI）与轮换用药防抗性要求，语气科学、客观、严谨有据。"
        )

        if context_text:
            return (
                f"{base_prompt}\n\n"
                "以下是从知识库中检索到的相关信息，请参考这些信息来回答用户问题：\n\n"
                "---知识库内容开始---\n"
                f"{context_text}\n"
                "---知识库内容结束---\n\n"
                "请基于上述知识库内容回答用户问题。如果知识库内容不足以回答问题，可以适当补充，但要保持专业和准确。"
            )
        return base_prompt

    def build_messages(
        self,
        user_message: str,
        context_text: Optional[str] = None,
        history: Optional[List[Dict]] = None
    ) -> List[Dict]:
        messages = [{"role": "system", "content": self.build_system_prompt(context_text)}]

        if history:
            for msg in history:
                role = msg.get("role")
                content = msg.get("content")
                if role in ("user", "assistant") and content:
                    messages.append({"role": role, "content": str(content)})

        messages.append({"role": "user", "content": user_message})
        return messages

    @property
    def last_result(self) -> Optional[RAGSearchResult]:
        return self._last_result
