# -*- coding: utf-8 -*-
"""
向量存储模块 - 使用FAISS进行向量存储和检索

优化内容 (v2.2.0):
- 向量维度不匹配时提供自动修复建议
- 添加索引状态检查方法

优化内容 (v2.3.0):
- 支持多种索引类型：Flat、IVF、HNSW
- 大规模数据自动切换压缩索引
- 索引类型可配置，支持精度/空间权衡
"""

import os
import re
import json
import hashlib
import time
import functools
import threading
import numpy as np
import logging
from datetime import datetime, timezone
from typing import List, Tuple, Optional, Dict, Set
from enum import Enum


logger = logging.getLogger(__name__)

try:
    import faiss
    FAISS_AVAILABLE = True
except ImportError:
    FAISS_AVAILABLE = False
    logger.warning("faiss-cpu未安装，将使用简单的numpy检索")


class IndexType(Enum):
    """索引类型枚举"""
    FLAT = "flat"
    IVF = "ivf"
    HNSW = "hnsw"
    AUTO = "auto"


INDEX_THRESHOLD_IVF = 1000
INDEX_THRESHOLD_HNSW = 50000


def _locked(method):
    """Serialize access to the shared singleton's mutable index/map state.

    VectorStore is a process-wide singleton and vector_service drives it from a
    ThreadPoolExecutor, so every state-touching operation must be mutually exclusive.
    Re-entrant (RLock) because e.g. remove_vector() may call rebuild_index().
    """
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper


def _mutating(method):
    """Like _locked, but also bumps the mutation counter.

    Milestone rebuilds build a candidate index outside the lock and only swap it
    in under a short lock; the counter lets them detect a write that landed
    during the build so it is never silently overwritten.
    """
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            self._mutation_seq += 1
            return method(self, *args, **kwargs)
    return wrapper


class VectorStore:
    """向量存储类 - 基于FAISS，支持多种索引类型"""
    
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        
        self._dimension = 1024
        self._index = None
        self._index_type: IndexType = IndexType.FLAT
        self._id_map: Dict[int, str] = {}
        self._reverse_map: Dict[str, int] = {}
        self._next_id = 0
        self._built_embedding_model: Optional[str] = None
        self._embedding_contract = None
        self._knowledge_fingerprint = None
        self._last_error: Optional[dict] = None
        self._pending_vectors: List[Tuple[int, np.ndarray]] = []
        self._is_trained = False

        # Guards all mutable index/id-map state; see _locked().
        self._lock = threading.RLock()

        self._generations_to_keep = 2
        # Monotonic counter bumped by every state mutation; rebuild uses it to
        # detect a write that raced with an off-lock rebuild.
        self._mutation_seq = 0

        # Setting _data_dir (a property) derives the manifest + legacy paths.
        self._data_dir = self._get_data_dir()
        
        self._load_index()
    
    def _get_data_dir(self) -> str:
        """获取数据目录（统一使用 settings.DATA_DIR）"""
        from app.config import settings
        data_dir = settings.DATA_DIR
        if not os.path.isabs(data_dir):
            base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            data_dir = os.path.join(base_dir, data_dir)
        os.makedirs(data_dir, exist_ok=True)
        return data_dir

    # ---- persistence layout -------------------------------------------------
    # A "generation" is the indivisible pair (faiss index + id map). manifest.json
    # is the single atomic switch that publishes a new generation; the previous
    # generation is retained as a rollback point. Legacy (pre-generation) files
    # stay readable but are never written again.
    @property
    def _data_dir(self) -> str:
        return self._data_dir_path

    @_data_dir.setter
    def _data_dir(self, path: str):
        self._data_dir_path = path
        self._active_manifest_descriptor = None
        self._manifest_file = os.path.join(path, "manifest.json")
        self._legacy_index_file = os.path.join(path, "vectors.index")
        self._legacy_map_file = os.path.join(path, "vectors_map.json")
        self._index_file = self._legacy_index_file
        self._map_file = self._legacy_map_file

    def _gen_paths(self, generation: str) -> Tuple[str, str]:
        """Return (index_path, map_path) for a generation id."""
        return (
            os.path.join(self._data_dir, f"vectors_gen_{generation}.index"),
            os.path.join(self._data_dir, f"vectors_gen_{generation}.map.json"),
        )

    def _abs_data_path(self, name: str) -> str:
        return name if os.path.isabs(name) else os.path.join(self._data_dir, name)

    def _get_current_embedding_model(self) -> Optional[str]:
        try:
            from app.core.config import Config
            return Config().get("embedding_model")
        except Exception:
            return None
    
    @staticmethod
    def _resolve_index_type(index_type: IndexType, expected_count: int) -> IndexType:
        if index_type != IndexType.AUTO:
            return index_type
        if expected_count >= INDEX_THRESHOLD_HNSW:
            return IndexType.HNSW
        if expected_count >= INDEX_THRESHOLD_IVF:
            return IndexType.IVF
        return IndexType.FLAT

    @staticmethod
    def _new_faiss_index(dimension: int, index_type: IndexType, expected_count: int):
        """Build a raw FAISS index for an already-resolved type.

        Returns (index_or_None, is_trained). index is None when FAISS is
        unavailable (numpy fallback path).
        """
        if not FAISS_AVAILABLE:
            return None, True
        if index_type == IndexType.HNSW:
            index = faiss.IndexHNSWFlat(dimension, 32)
            index.hnsw.efConstruction = 200
            index.hnsw.efSearch = 64
            return index, True
        if index_type == IndexType.IVF:
            nlist = max(16, min(256, int(np.sqrt(max(expected_count, 1000)))))
            quantizer = faiss.IndexFlatIP(dimension)
            index = faiss.IndexIVFFlat(quantizer, dimension, nlist, faiss.METRIC_INNER_PRODUCT)
            return index, False
        base_index = faiss.IndexFlatIP(dimension)
        return faiss.IndexIDMap2(base_index), True

    def _create_index(self, dimension: int, embedding_model: Optional[str] = None, 
                       index_type: IndexType = IndexType.AUTO, expected_count: int = 0):
        self._dimension = dimension
        if embedding_model is not None:
            self._built_embedding_model = embedding_model
        elif self._built_embedding_model is None:
            self._built_embedding_model = self._get_current_embedding_model()
        
        if index_type == IndexType.AUTO:
            if expected_count >= INDEX_THRESHOLD_HNSW:
                index_type = IndexType.HNSW
            elif expected_count >= INDEX_THRESHOLD_IVF:
                index_type = IndexType.IVF
            else:
                index_type = IndexType.FLAT
        
        self._index_type = index_type
        self._pending_vectors = []
        self._is_trained = False
        
        if FAISS_AVAILABLE:
            if index_type == IndexType.HNSW:
                self._index = faiss.IndexHNSWFlat(dimension, 32)
                self._index.hnsw.efConstruction = 200
                self._index.hnsw.efSearch = 64
                self._is_trained = True
                logger.info(f"创建HNSW索引，维度: {dimension}")
            elif index_type == IndexType.IVF:
                nlist = max(16, min(256, int(np.sqrt(max(expected_count, 1000)))))
                quantizer = faiss.IndexFlatIP(dimension)
                self._index = faiss.IndexIVFFlat(quantizer, dimension, nlist, faiss.METRIC_INNER_PRODUCT)
                self._is_trained = False
                logger.info(f"创建IVF索引，维度: {dimension}, nlist: {nlist}")
            else:
                base_index = faiss.IndexFlatIP(dimension)
                self._index = faiss.IndexIDMap2(base_index)
                self._is_trained = True
                logger.info(f"创建Flat索引，维度: {dimension}")
        else:
            self._vectors = []
            self._is_trained = True
    
    @_locked
    def bind_empty_contract(self, contract):
        from app.core.embedding_contract import validate_contract
        if not self._id_map:
            self._embedding_contract = validate_contract(contract)
            self._built_embedding_model = contract["model"]
            if self._dimension != contract["dimension"]:
                self._create_index(contract["dimension"], embedding_model=contract["model"])

    @_locked
    def revision(self) -> int:
        """Version captured before DB reads/embedding collection."""
        return self._mutation_seq

    def _snapshot_state(self) -> dict:
        return {
            "index": self._index, "id_map": dict(self._id_map),
            "reverse_map": dict(self._reverse_map), "next_id": self._next_id,
            "dimension": self._dimension, "embedding_model": self._built_embedding_model,
            "embedding_contract": getattr(self, "_embedding_contract", None),
            "knowledge_fingerprint": getattr(self, "_knowledge_fingerprint", None),
            "index_type": self._index_type, "is_trained": self._is_trained,
            "vectors": list(getattr(self, "_vectors", [])),
        }

    def _install_state(self, state: dict):
        self._index = state["index"]
        self._id_map = state["id_map"]
        self._reverse_map = state["reverse_map"]
        self._next_id = state["next_id"]
        self._dimension = state["dimension"]
        self._built_embedding_model = state["embedding_model"]
        self._embedding_contract = state.get("embedding_contract")
        self._knowledge_fingerprint = state.get("knowledge_fingerprint")
        self._index_type = state["index_type"]
        self._is_trained = state["is_trained"]
        self._pending_vectors = []
        self._vectors = state.get("vectors", [])
        self._last_error = None

    @staticmethod
    def _file_digest(path: str) -> str:
        digest = hashlib.sha256()
        with open(path, "rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def _descriptor_paths(self, descriptor: dict) -> Tuple[str, str]:
        generation = descriptor.get("generation")
        if not isinstance(generation, str) or not re.fullmatch(r"\d{20}", generation):
            raise ValueError("Invalid generation ID")
        index_path, map_path = self._gen_paths(generation)
        # Manifest filenames cannot escape the data directory or select another pair.
        if descriptor.get("index_file") != os.path.basename(index_path):
            raise ValueError("Generation index filename mismatch")
        if descriptor.get("map_file") != os.path.basename(map_path):
            raise ValueError("Generation map filename mismatch")
        return map_path, index_path

    def _read_manifest(self, path: Optional[str] = None) -> Optional[dict]:
        try:
            with open(path or self._manifest_file, "r", encoding="utf-8") as stream:
                data = json.load(stream)
            if not isinstance(data, dict) or data.get("version") not in (1, 2):
                raise ValueError("Invalid manifest")
            self._descriptor_paths(data)
            return data
        except FileNotFoundError:
            return None
        except Exception:
            logger.warning("Invalid vector manifest; trying a published predecessor", exc_info=True)
            return None

    @_locked
    def _load_index(self):
        """Load only a complete validated published pair; recover its predecessor."""
        candidates = []
        current = self._read_manifest()
        backup_path = self._manifest_file + ".previous"
        backup = self._read_manifest(backup_path)
        rejected = ((os.path.exists(self._manifest_file) and current is None)
                    or (os.path.exists(backup_path) and backup is None))
        for manifest in (current, backup):
            if manifest:
                candidates.append(manifest)
                previous = manifest.get("previous")
                if isinstance(previous, dict):
                    candidates.append(previous)
        visited = set()
        for descriptor in candidates + [None]:
            try:
                if descriptor is None:
                    map_path, index_path = self._legacy_map_file, self._legacy_index_file
                else:
                    map_path, index_path = self._descriptor_paths(descriptor)
                if (map_path, index_path) in visited:
                    continue
                visited.add((map_path, index_path))
                if not os.path.exists(map_path) and not os.path.exists(index_path):
                    if descriptor is not None:
                        rejected = True
                    continue
                state = self._load_from_paths(map_path, index_path, descriptor)
                self._install_state(state)
                self._map_file, self._index_file = map_path, index_path
                self._active_manifest_descriptor = descriptor
                self._mutation_seq += 1
                if rejected:
                    self._last_error = {"type": "generation_recovered",
                                        "message": "Recovered a complete previous vector generation"}
                return
            except Exception:
                rejected = True
                logger.warning("Rejected incomplete or inconsistent vector generation", exc_info=True)
        self._id_map, self._reverse_map, self._next_id = {}, {}, 0
        self._index_file, self._map_file = self._legacy_index_file, self._legacy_map_file
        self._active_manifest_descriptor = None
        self._create_index(self._dimension)
        self._mutation_seq += 1
        if rejected:
            self._last_error = {"type": "invalid_generation",
                                "message": "No complete vector generation could be loaded"}

    def _load_from_paths(self, map_path: str, index_path: str,
                         descriptor: Optional[dict] = None) -> dict:
        """Validate off-state. A rejected pair never partially mutates live maps."""
        if descriptor and descriptor.get("version") == 2:
            if self._file_digest(map_path) != descriptor.get("map_sha256"):
                raise ValueError("Map checksum mismatch")
            if self._file_digest(index_path) != descriptor.get("index_sha256"):
                raise ValueError("Index checksum mismatch")
        with open(map_path, "r", encoding="utf-8") as stream:
            data = json.load(stream)
        if not isinstance(data, dict):
            raise ValueError("Map must be an object")
        if descriptor and data.get("format_version") == 2:
            if data.get("generation") != descriptor["generation"]:
                raise ValueError("Generation metadata mismatch")
            if self._file_digest(index_path) != data.get("index_sha256"):
                raise ValueError("Map/index pair checksum mismatch")
        raw_map, reverse = data.get("id_map"), data.get("reverse_map")
        if not isinstance(raw_map, dict) or not isinstance(reverse, dict):
            raise ValueError("Missing ID maps")
        id_map = {}
        for key, item in raw_map.items():
            if (not isinstance(key, str) or not re.fullmatch(r"0|[1-9]\d*", key)
                    or not isinstance(item, str) or not item):
                raise ValueError("Invalid ID map entry")
            id_map[int(key)] = item
        if len(set(id_map.values())) != len(id_map):
            raise ValueError("Duplicate item IDs")
        if any(type(value) is not int or value < 0 for value in reverse.values()):
            raise ValueError("Reverse IDs must be integers")
        if reverse != {item: internal for internal, item in id_map.items()}:
            raise ValueError("Forward/reverse map mismatch")
        dimension, next_id = data.get("dimension"), data.get("next_id")
        if type(dimension) is not int or not 0 < dimension <= 65536:
            raise ValueError("Invalid dimension")
        if type(next_id) is not int or next_id < 0 or next_id <= max(id_map, default=-1):
            raise ValueError("Invalid next ID")
        if type(data.get("is_trained")) is not bool:
            raise ValueError("Invalid training state")
        from app.core.embedding_contract import validate_contract
        contract = validate_contract(data.get("embedding_contract"))
        if contract and contract["dimension"] != dimension: raise ValueError("Contract dimension mismatch")
        source_fingerprint = data.get("knowledge_fingerprint")
        if source_fingerprint is not None and (not isinstance(source_fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", source_fingerprint)):
            raise ValueError("Invalid knowledge fingerprint")
        model = data.get("embedding_model")
        if contract and model != contract["model"]: raise ValueError("Contract model mismatch")
        if model is not None and not isinstance(model, str):
            raise ValueError("Invalid embedding model")
        index_type = IndexType(data.get("index_type"))
        if FAISS_AVAILABLE:
            loaded = faiss.read_index(index_path)
            if isinstance(loaded, (faiss.IndexIDMap, faiss.IndexIDMap2)):
                actual_type = IndexType.FLAT
                native_ids = set(int(i) for i in faiss.vector_to_array(loaded.id_map))
            elif isinstance(loaded, faiss.IndexHNSWFlat):
                actual_type, native_ids = IndexType.HNSW, set(range(loaded.ntotal))
            elif isinstance(loaded, faiss.IndexIVFFlat):
                actual_type = IndexType.IVF
                native_ids = set()
                for bucket in range(loaded.nlist):
                    size = loaded.invlists.list_size(bucket)
                    pointer = loaded.invlists.get_ids(bucket)
                    try:
                        native_ids.update(int(i) for i in faiss.rev_swig_ptr(pointer, size))
                    finally:
                        loaded.invlists.release_ids(bucket, pointer)
            elif isinstance(loaded, faiss.IndexFlat):
                # Legacy position-indexed FLAT files are supported only with exact maps.
                actual_type, native_ids = IndexType.FLAT, set(range(loaded.ntotal))
                wrapped = faiss.IndexIDMap2(faiss.IndexFlatIP(dimension))
                if loaded.ntotal:
                    vectors = loaded.reconstruct_n(0, loaded.ntotal)
                    wrapped.add_with_ids(vectors, np.arange(loaded.ntotal, dtype=np.int64))
                loaded = wrapped
            else:
                raise ValueError("Unsupported native index type")
            if (loaded.d != dimension or actual_type != index_type
                    or loaded.ntotal != len(id_map) or native_ids != set(id_map)
                    or bool(loaded.is_trained) != data["is_trained"]):
                raise ValueError("Native index/maps/dimension/type mismatch")
            vectors = []
        else:
            with open(index_path, "rb") as stream:
                matrix = np.load(stream, allow_pickle=False)
            if (matrix.ndim != 2 or matrix.shape != (len(id_map), dimension)
                    or set(id_map) != set(range(len(id_map))) or not np.isfinite(matrix).all()):
                raise ValueError("Invalid numpy vector snapshot")
            vectors, loaded = list(matrix), None
        return dict(index=loaded, id_map=id_map, reverse_map=reverse, next_id=next_id,
                    dimension=dimension, embedding_model=model, embedding_contract=contract, knowledge_fingerprint=data.get("knowledge_fingerprint"), index_type=index_type,
                    is_trained=data["is_trained"], vectors=vectors)

    @_locked
    def _save_index(self) -> bool:
        state = self._snapshot_state()
        if not FAISS_AVAILABLE:
            # Logical deletes leave holes in numpy's positional array. Persist
            # a compact complete snapshot without rewriting the live positions.
            vectors = self._snapshot_vectors()
            if len(vectors) != len(self._id_map):
                self._last_error = {"type": "persistence_failure",
                                    "message": "Incomplete numpy vector snapshot"}
                return False
            state = self._build_index_objects(vectors, self._index_type, self._dimension)
            state.update(dimension=self._dimension, embedding_model=self._built_embedding_model,
                         embedding_contract=getattr(self, "_embedding_contract", None), knowledge_fingerprint=getattr(self, "_knowledge_fingerprint", None))
        ok = self._persist_state(state)
        if not ok:
            self._last_error = {"type": "persistence_failure",
                                "message": "In-memory updates are not persisted; retry save"}
        elif self._last_error and self._last_error.get("type") == "persistence_failure":
            self._last_error = None
        return ok

    def _persist_state(self, state: dict) -> bool:
        """Persist a candidate before publishing it in memory; one manifest commits."""
        generation = f"{time.time_ns():020d}"
        if generation <= getattr(self, "_last_generation", ""):
            generation = f"{int(self._last_generation) + 1:020d}"
        self._last_generation = generation
        index_path, map_path = self._gen_paths(generation)
        tmp_index, tmp_map = index_path + ".tmp", map_path + ".tmp"
        data = {
            "format_version": 2, "generation": generation,
            "id_map": {str(k): value for k, value in state["id_map"].items()},
            "reverse_map": state["reverse_map"], "next_id": state["next_id"],
            "dimension": state["dimension"], "embedding_model": state["embedding_model"],
            "embedding_contract": state.get("embedding_contract"),
            "knowledge_fingerprint": state.get("knowledge_fingerprint"),
            "index_type": state["index_type"].value, "is_trained": state["is_trained"],
        }
        try:
            if FAISS_AVAILABLE:
                faiss.write_index(state["index"], tmp_index)
                with open(tmp_index, "r+b") as stream:
                    os.fsync(stream.fileno())
            else:
                matrix = np.asarray(state["vectors"], dtype=np.float32).reshape(
                    len(state["id_map"]), state["dimension"])
                with open(tmp_index, "wb") as stream:
                    np.save(stream, matrix, allow_pickle=False)
                    stream.flush()
                    os.fsync(stream.fileno())
            os.replace(tmp_index, index_path)
            data["index_sha256"] = self._file_digest(index_path)
            with open(tmp_map, "w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp_map, map_path)
            # Read back and validate before the commit point, including native IDs.
            self._load_from_paths(map_path, index_path)
            self._sync_directory()
            if not self._write_manifest(generation, index_path, map_path):
                raise OSError("Manifest publication failed")
        except Exception:
            logger.exception("Vector generation publication failed; previous generation retained")
            self._remove_files(tmp_index, tmp_map, index_path, map_path)
            return False
        self._index_file, self._map_file = index_path, map_path
        self._gc_generations(keep=generation)
        return True

    def _sync_directory(self):
        # Windows does not expose directory fsync through os.open. File fsync and
        # atomic os.replace remain mandatory there; POSIX also syncs directory entries.
        if os.name != "nt":
            fd = os.open(self._data_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    def _write_manifest(self, generation: str, index_path: str, map_path: str) -> bool:
        previous = getattr(self, "_active_manifest_descriptor", None) or self._read_manifest()
        data = dict(version=2, generation=generation, index_file=os.path.basename(index_path),
                    map_file=os.path.basename(map_path), created_at=datetime.now(timezone.utc).isoformat(),
                    index_sha256=self._file_digest(index_path), map_sha256=self._file_digest(map_path))
        if previous:
            data["previous"] = {key: value for key, value in previous.items() if key != "previous"}
        tmp = self._manifest_file + ".tmp"
        backup = self._manifest_file + ".previous"
        try:
            if previous:
                with open(backup + ".tmp", "w", encoding="utf-8") as stream:
                    json.dump(previous, stream, ensure_ascii=False)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(backup + ".tmp", backup)
            with open(tmp, "w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, self._manifest_file)
        except Exception:
            logger.exception("Manifest commit failed; previous generation retained")
            self._remove_files(tmp, backup + ".tmp")
            return False
        self._active_manifest_descriptor = data
        # After os.replace the publication is committed. Do not report an abort
        # or delete its files if a post-commit directory sync is unsupported/fails.
        try:
            self._sync_directory()
        except OSError:
            logger.warning("Manifest committed but directory sync failed", exc_info=True)
        return True

    @staticmethod
    def _remove_files(*paths: str):
        for path in paths:
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                logger.warning("清理残片失败: %s", path, exc_info=True)

    def _gc_generations(self, keep: str):
        """Delete generations older than the retained rollback window.

        The freshly published generation and its most recent predecessor are
        kept so a bad publish can still be rolled back; only strictly older
        generations are removed. Legacy (pre-generation) files are never touched.
        """
        try:
            found: Dict[str, List[str]] = {}
            for name in os.listdir(self._data_dir):
                match = re.match(r"^vectors_gen_(\d+)\.(?:index|map\.json)$", name)
                if match:
                    found.setdefault(match.group(1), []).append(name)
            ordered = sorted(found.keys(), reverse=True)
            retained = set(ordered[: self._generations_to_keep]) | {keep}
            for generation in ordered:
                if generation in retained:
                    continue
                self._remove_files(*[os.path.join(self._data_dir, n) for n in found[generation]])
                logger.info("清理过期向量代际: %s", generation)
        except Exception:
            logger.exception("清理过期代际失败（不影响保存结果）")
    
    @_mutating
    def add_vector(self, item_id: str, vector: List[float], *, expected_contract=None) -> bool:
        if expected_contract is not None:
            if len(vector) != expected_contract["dimension"]: return False
            if self._id_map and getattr(self, "_embedding_contract", None) != expected_contract: return False
            self.bind_empty_contract(expected_contract)
        if not vector:
            return False
        if len(vector) != self._dimension:
            if not self._id_map:
                self._create_index(len(vector), embedding_model=self._get_current_embedding_model())
            else:
                self._last_error = {
                    "type": "dimension_mismatch",
                    "expected": self._dimension,
                    "actual": len(vector),
                    "op": "add_vector",
                }
                logger.warning("向量维度不匹配: 期望%s, 实际%s", self._dimension, len(vector))
                return False
        if item_id in self._reverse_map:
            self.remove_vector(item_id)
        vec = np.array([vector], dtype=np.float32)
        if FAISS_AVAILABLE:
            faiss.normalize_L2(vec)
        internal_id = self._next_id
        self._next_id += 1
        if FAISS_AVAILABLE:
            if self._index_type == IndexType.IVF and not self._is_trained:
                self._pending_vectors.append((internal_id, vec[0].copy()))
                logger.debug(f"IVF索引未训练，暂存向量 {item_id}")
            elif self._index_type == IndexType.HNSW:
                self._index.add(vec)
            elif self._index_type == IndexType.FLAT:
                ids = np.array([internal_id], dtype=np.int64)
                self._index.add_with_ids(vec, ids)
            else:
                self._index.add(vec)
        else:
            if not hasattr(self, '_vectors'):
                self._vectors = []
            self._vectors.append(vec[0])
        self._id_map[internal_id] = item_id
        self._reverse_map[item_id] = internal_id
        return True
    
    @_mutating
    def train_index(self, vectors: Optional[np.ndarray] = None) -> bool:
        if not FAISS_AVAILABLE:
            return False
        if self._index_type != IndexType.IVF:
            return True
        if self._is_trained:
            return True
        if vectors is None:
            if not self._pending_vectors:
                logger.warning("没有可用的训练数据")
                return False
            vectors = np.array([v for _, v in self._pending_vectors], dtype=np.float32)
        min_train_size = max(self._index.nlist, 39)
        if len(vectors) < min_train_size:
            logger.warning(f"训练数据不足: {len(vectors)} < {min_train_size}")
            return False
        try:
            logger.info(f"开始训练IVF索引，训练数据: {len(vectors)} 条")
            self._index.train(vectors)
            self._is_trained = True
            if self._pending_vectors:
                for internal_id, vec in self._pending_vectors:
                    self._index.add(vec.reshape(1, -1))
                logger.info(f"已添加 {len(self._pending_vectors)} 条暂存向量")
                self._pending_vectors = []
            return True
        except Exception as e:
            logger.exception("训练IVF索引失败")
            return False

    @_locked
    def _snapshot_vectors(self, exclude_internal_ids: Optional[Set[int]] = None) -> List[Tuple[str, List[float]]]:
        """提取当前索引中的向量快照，用于删除后重建索引。"""
        exclude = exclude_internal_ids or set()
        vectors_data: List[Tuple[str, List[float]]] = []

        if FAISS_AVAILABLE and self._index_type == IndexType.IVF and not self._is_trained:
            for internal_id, vec in self._pending_vectors:
                if internal_id in exclude:
                    continue
                item_id = self._id_map.get(internal_id)
                if item_id:
                    vectors_data.append((item_id, vec.tolist()))
            return vectors_data

        if not FAISS_AVAILABLE:
            if not hasattr(self, '_vectors'):
                return vectors_data
            for internal_id, item_id in sorted(self._id_map.items()):
                if internal_id in exclude:
                    continue
                if 0 <= internal_id < len(self._vectors):
                    vectors_data.append((item_id, self._vectors[internal_id].tolist()))
            return vectors_data

        if self._index is None or getattr(self._index, "ntotal", 0) <= 0:
            return vectors_data

        ntotal = int(self._index.ntotal)
        # NOTE: reconstruct_n() aborts (native crash) on IndexIDMap/IndexIDMap2 — only use
        # it for position-indexed types; for ID-mapped indexes extract per internal id.
        is_idmap = FAISS_AVAILABLE and isinstance(self._index, (faiss.IndexIDMap, faiss.IndexIDMap2))
        if hasattr(self._index, "reconstruct_n") and not is_idmap:
            try:
                vectors = self._index.reconstruct_n(0, ntotal)
                for internal_id, vec in enumerate(vectors):
                    if internal_id in exclude:
                        continue
                    item_id = self._id_map.get(internal_id)
                    if item_id:
                        vectors_data.append((item_id, vec.tolist()))
                return vectors_data
            except Exception:
                logger.exception("批量提取向量快照失败，回退到逐条提取")

        for internal_id, item_id in sorted(self._id_map.items()):
            if internal_id in exclude:
                continue
            try:
                vec = self._index.reconstruct(internal_id)
                vectors_data.append((item_id, np.asarray(vec, dtype=np.float32).tolist()))
            except Exception:
                logger.exception("提取向量失败: %s", item_id)

        return vectors_data
    
    @_mutating
    def remove_vector(self, item_id: str) -> bool:
        if item_id not in self._reverse_map:
            return False
        internal_id = self._reverse_map[item_id]
        if FAISS_AVAILABLE and self._index is not None and self._index_type == IndexType.FLAT:
            try:
                remove_ids = np.array([internal_id], dtype=np.int64)
                self._index.remove_ids(remove_ids)
            except Exception as e:
                logger.exception("FAISS删除向量失败")
        elif FAISS_AVAILABLE and self._index is not None and self._index_type in (IndexType.IVF, IndexType.HNSW):
            vectors_data = self._snapshot_vectors(exclude_internal_ids={internal_id})
            if self.rebuild_index(index_type=IndexType.AUTO, vectors_data=vectors_data):
                return True
            logger.warning("非FLAT索引删除重建失败，回退到逻辑删除: %s", item_id)
        if self._pending_vectors:
            self._pending_vectors = [(id, v) for id, v in self._pending_vectors if id != internal_id]
        if internal_id in self._id_map:
            del self._id_map[internal_id]
        del self._reverse_map[item_id]
        return True

    @_mutating
    def remove_vectors_by_prefix(self, prefix: str) -> int:
        if not prefix:
            return 0
        keys = list(self._reverse_map.keys())
        removed = 0
        for k in keys:
            if k == prefix or k.startswith(prefix):
                if self.remove_vector(k):
                    removed += 1
        return removed
    
    @_locked
    def search(self, query_vector: List[float], top_k: int = 5, *, expected_contract=None) -> List[Tuple[str, float]]:
        if expected_contract is not None and getattr(self, "_embedding_contract", None) != expected_contract: return []
        if not query_vector:
            return []
        if len(query_vector) != self._dimension:
            self._last_error = {
                "type": "dimension_mismatch",
                "expected": self._dimension,
                "actual": len(query_vector),
                "op": "search",
            }
            logger.warning("查询向量维度不匹配: 索引维度%s, 查询维度%s", self._dimension, len(query_vector))
            return []
        if FAISS_AVAILABLE and (self._index is None or self._index.ntotal == 0):
            return []
        if self._index_type == IndexType.IVF and not self._is_trained:
            return self._search_pending_vectors(query_vector, top_k)
        vec = np.array([query_vector], dtype=np.float32)
        if FAISS_AVAILABLE:
            faiss.normalize_L2(vec)
        results = []
        if FAISS_AVAILABLE:
            if self._index_type == IndexType.IVF:
                self._index.nprobe = min(16, self._index.nlist)
            k = min(top_k * 2, self._index.ntotal)
            try:
                distances, ids = self._index.search(vec, k)
            except AssertionError:
                logger.exception("FAISS搜索维度断言失败")
                return []
            for dist, internal_id in zip(distances[0], ids[0]):
                if internal_id == -1:
                    continue
                kid = self._id_map.get(int(internal_id))
                if not kid:
                    continue
                results.append((kid, float(dist)))
                if len(results) >= top_k:
                    break
        else:
            if not hasattr(self, '_vectors') or not self._vectors:
                return []
            vectors = np.array(self._vectors)
            norms = np.linalg.norm(vectors, axis=1, keepdims=True)
            vectors = vectors / (norms + 1e-9)
            query = vec[0] / (np.linalg.norm(vec[0]) + 1e-9)
            similarities = np.dot(vectors, query)
            top_indices = np.argsort(similarities)[::-1][:top_k]
            for idx in top_indices:
                if idx in self._id_map:
                    results.append((self._id_map[idx], float(similarities[idx])))
        return results
    
    def _search_pending_vectors(self, query_vector: List[float], top_k: int) -> List[Tuple[str, float]]:
        if not self._pending_vectors:
            return []
        query = np.array(query_vector, dtype=np.float32)
        query = query / (np.linalg.norm(query) + 1e-9)
        scores = []
        for internal_id, vec in self._pending_vectors:
            similarity = np.dot(vec, query)
            kid = self._id_map.get(internal_id)
            if kid:
                scores.append((kid, float(similarity)))
        scores.sort(key=lambda x: x[1], reverse=True)
        return scores[:top_k]
    
    @_locked
    def save(self) -> bool:
        ok = self._save_index()
        if ok:
            logger.info("向量索引已保存，共%s条", len(self._id_map))
        else:
            logger.error("向量索引保存失败，未伪报成功")
        return ok
    
    @_mutating
    def clear(self):
        self._create_index(self._dimension, embedding_model=self._get_current_embedding_model())
        self._id_map.clear()
        self._reverse_map.clear()
        self._next_id = 0
        self._last_error = None
        self._pending_vectors = []
        if hasattr(self, '_vectors'):
            self._vectors = []
    
    @property
    @_locked
    def count(self) -> int:
        return len(self._id_map)
    
    @_locked
    def has_item(self, item_id: str) -> bool:
        return item_id in self._reverse_map

    @property
    @_locked
    def dimension(self) -> int:
        return int(self._dimension)

    @property
    @_locked
    def embedding_model(self) -> Optional[str]:
        return self._built_embedding_model

    @property
    @_locked
    def last_error(self) -> Optional[dict]:
        return self._last_error

    @_locked
    def check_dimension_compatibility(self, expected_dimension: int) -> Tuple[bool, str]:
        if not self._id_map:
            return True, "索引为空，可以使用任意维度"
        if self._dimension == expected_dimension:
            return True, "维度匹配"
        return False, f"维度不匹配：索引维度为 {self._dimension}，当前模型维度为 {expected_dimension}。请重建向量索引。"
    
    @_locked
    def needs_rebuild(self, current_embedding_model: str = None) -> Tuple[bool, str]:
        if self._last_error and self._last_error.get("type") == "invalid_generation":
            return True, "Invalid vector generation; rebuild required"
        if (FAISS_AVAILABLE and (self._index is None or self._index.ntotal == 0)) or (
                not FAISS_AVAILABLE and not getattr(self, "_vectors", [])):
            if len(self._id_map) == 0:
                return False, "索引为空，无需重建"
            return True, "索引数据丢失，需要重建"
        if current_embedding_model and self._built_embedding_model:
            if current_embedding_model != self._built_embedding_model:
                return True, f"Embedding模型已变更（{self._built_embedding_model} → {current_embedding_model}），建议重建索引"
        if self._last_error and self._last_error.get("type") == "dimension_mismatch":
            return True, f"向量维度不匹配，需要重建索引"
        return False, "索引状态正常"
    
    @_locked
    def get_index_info(self) -> dict:
        info = {
            "dimension": self._dimension,
            "count": len(self._id_map),
            "embedding_model": self._built_embedding_model,
            "embedding_contract": getattr(self, "_embedding_contract", None),
            "knowledge_fingerprint": getattr(self, "_knowledge_fingerprint", None),
            "generation": (getattr(self, "_active_manifest_descriptor", None) or {}).get("generation"),
            "faiss_available": FAISS_AVAILABLE,
            "has_error": self._last_error is not None,
            "last_error": self._last_error,
            "index_type": self._index_type.value,
            "is_trained": self._is_trained,
            "pending_count": len(self._pending_vectors),
        }
        if FAISS_AVAILABLE and self._index is not None:
            info["ntotal"] = self._index.ntotal
            if self._index_type == IndexType.IVF:
                info["nlist"] = self._index.nlist
                info["nprobe"] = getattr(self._index, 'nprobe', None)
            elif self._index_type == IndexType.HNSW:
                if hasattr(self._index, 'hnsw'):
                    info["M"] = self._index.hnsw.M if hasattr(self._index.hnsw, 'M') else 32
                    info["efConstruction"] = self._index.hnsw.efConstruction
                    info["efSearch"] = self._index.hnsw.efSearch
        return info
    
    def rebuild_index(self, index_type: IndexType = IndexType.AUTO,
                      vectors_data: Optional[List[Tuple[str, List[float]]]] = None,
                      *, expected_revision: Optional[int] = None,
                      embedding_model: Optional[str] = None, persist: bool = False,
                      validate_config=None, embedding_contract=None, knowledge_fingerprint=None) -> bool:
        """Build off-state, reject writes since collection, persist, then publish."""
        with self._lock:
            if vectors_data is None:
                vectors_data = self._snapshot_vectors()
                if len(vectors_data) != len(self._id_map):
                    logger.error("Incomplete native snapshot; rebuild rejected")
                    return False
            data = list(vectors_data)
            seq = self._mutation_seq if expected_revision is None else expected_revision
            dimension = len(data[0][1]) if data else embedding_contract["dimension"] if embedding_contract else self._dimension
            model = embedding_model if embedding_model is not None else self._built_embedding_model
        try:
            if type(dimension) is not int or not 0 < dimension <= 65536:
                raise ValueError("Invalid vector dimension")
            built = self._build_index_objects(data, index_type, dimension)
            from app.core.embedding_contract import validate_contract
            contract = validate_contract(embedding_contract)
            if contract and contract["dimension"] != dimension: raise ValueError("Contract dimension mismatch")
            built.update(dimension=dimension, embedding_model=model, embedding_contract=contract, knowledge_fingerprint=knowledge_fingerprint)
        except Exception:
            logger.exception("Candidate index build failed; old index retained")
            return False
        with self._lock:
            if self._mutation_seq != seq or (validate_config and not validate_config()):
                logger.info("Collection/build raced with a write/config change; candidate rejected")
                return False
            if persist and not self._persist_state(built):
                return False
            self._install_state(built)
            self._mutation_seq += 1
        logger.info("Published complete vector generation: count=%s, dimension=%s", len(data), dimension)
        return True

    def _build_index_objects(self, vectors_data, index_type: IndexType, dimension: int) -> dict:
        """Build a fresh (index, id_map, reverse_map) entirely off shared state."""
        data = list(vectors_data or [])
        seen = set()
        for item_id, vector in data:
            if not isinstance(item_id, str) or not item_id or item_id in seen:
                raise ValueError("Invalid or duplicate item ID")
            seen.add(item_id)
            array = np.asarray(vector, dtype=np.float32)
            if array.ndim != 1 or len(array) != dimension or not np.isfinite(array).all():
                raise ValueError("Invalid vector shape/value")
        resolved = self._resolve_index_type(index_type, len(data))
        index, is_trained = self._new_faiss_index(dimension, resolved, len(data))
        id_map: Dict[int, str] = {}
        reverse_map: Dict[str, int] = {}
        vectors: List[np.ndarray] = []
        next_id = 0
        if FAISS_AVAILABLE:
            all_vectors = []
            for _item_id, vector in data:
                vec = np.array(vector, dtype=np.float32)
                faiss.normalize_L2(vec.reshape(1, -1))
                all_vectors.append(vec)
            if resolved == IndexType.IVF and all_vectors:
                train_data = np.array(all_vectors, dtype=np.float32)
                if len(train_data) >= 39:
                    index.train(train_data)
                    is_trained = True
            for (item_id, _vector), vec in zip(data, all_vectors):
                if resolved == IndexType.FLAT:
                    index.add_with_ids(vec.reshape(1, -1), np.array([next_id], dtype=np.int64))
                else:
                    index.add(vec.reshape(1, -1))
                id_map[next_id] = item_id
                reverse_map[item_id] = next_id
                next_id += 1
        else:
            for item_id, vector in data:
                vectors.append(np.array(vector, dtype=np.float32))
                id_map[next_id] = item_id
                reverse_map[item_id] = next_id
                next_id += 1
        return {
            "index": index,
            "id_map": id_map,
            "reverse_map": reverse_map,
            "next_id": next_id,
            "index_type": resolved,
            "is_trained": is_trained,
            "vectors": vectors,
        }
    
    def optimize_index(self) -> Tuple[bool, str]:
        current_count = len(self._id_map)
        recommended_type = IndexType.FLAT
        if current_count >= INDEX_THRESHOLD_HNSW:
            recommended_type = IndexType.HNSW
        elif current_count >= INDEX_THRESHOLD_IVF:
            recommended_type = IndexType.IVF
        if recommended_type == self._index_type:
            return False, f"当前索引类型 {self._index_type.value} 已是最优选择"
        return True, f"建议将索引类型从 {self._index_type.value} 切换为 {recommended_type.value}（数据量: {current_count}）"
    
    def get_index_size_estimate(self) -> dict:
        count = len(self._id_map)
        dim = self._dimension
        flat_size = count * dim * 4
        ivf_size = flat_size * 1.1
        hnsw_size = flat_size * 1.5
        return {
            "count": count,
            "dimension": dim,
            "current_type": self._index_type.value,
            "estimated_sizes": {
                "flat": f"{flat_size / 1024 / 1024:.2f} MB",
                "ivf": f"{ivf_size / 1024 / 1024:.2f} MB",
                "hnsw": f"{hnsw_size / 1024 / 1024:.2f} MB",
            },
            "recommendations": {
                "flat": f"适合 < {INDEX_THRESHOLD_IVF} 条数据",
                "ivf": f"适合 {INDEX_THRESHOLD_IVF} - {INDEX_THRESHOLD_HNSW} 条数据",
                "hnsw": f"适合 > {INDEX_THRESHOLD_HNSW} 条数据",
            }
        }
    
    @property
    def index_type(self) -> IndexType:
        return self._index_type
    
    @property
    def is_trained(self) -> bool:
        return self._is_trained
