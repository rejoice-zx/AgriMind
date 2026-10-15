"""Agriculture Domain Schemas - 农业智能系统数据契约与验证模型"""
from pydantic import BaseModel, Field as PydanticField, ConfigDict, model_validator
import math
from typing import Optional, List, Any, Literal
from datetime import date, datetime
from uuid import UUID
from app.schemas.field_environment import FacilityType


# ==================== Farm Schemas ====================

class CoordinateInput(BaseModel):
    @model_validator(mode='before')
    @classmethod
    def required_asset_values(cls, data):
        if not isinstance(data, dict): return data
        data = dict(data)
        if any(key in data and data[key] is None for key in ('name', 'area')):
            raise ValueError('名称和面积不能显式设为null；不修改时请省略字段')
        if isinstance(data.get('name'), str): data['name'] = data['name'].strip()
        return data

    @model_validator(mode='before')
    @classmethod
    def coordinate_pair(cls, data):
        if not isinstance(data, dict): return data
        lat, lon = data.get('latitude'), data.get('longitude')
        if ('latitude' in data) != ('longitude' in data) or (lat is None) != (lon is None):
            raise ValueError('经纬度必须成对提交')
        for value, bound in ((lat, 90), (lon, 180)):
            if value is not None:
                try: number = float(value)
                except (TypeError, ValueError): raise ValueError('经纬度必须是数值') from None
                if isinstance(value, bool) or not math.isfinite(number) or not -bound <= number <= bound:
                    raise ValueError('经纬度必须是有效范围内的有限数值')
        return data

class FarmBase(BaseModel):
    name: str = PydanticField(..., min_length=1, max_length=100, description="农场名称")
    description: Optional[str] = PydanticField(None, description="农场描述")
    province: Optional[str] = PydanticField(None, max_length=50, description="省份")
    city: Optional[str] = PydanticField(None, max_length=50, description="城市")
    district: Optional[str] = PydanticField(None, max_length=50, description="区/县")
    address: Optional[str] = PydanticField(None, max_length=255, description="详细地址")
    region_code: Optional[str] = PydanticField(None, max_length=12)
    location_source: Optional[Literal['manual', 'region_reference']] = None
    location_label: Optional[str] = PydanticField(None, max_length=255)
    latitude: Optional[float] = PydanticField(None, ge=-90, le=90, description="纬度")
    longitude: Optional[float] = PydanticField(None, ge=-180, le=180, description="经度")
    area: float = PydanticField(0.0, ge=0, allow_inf_nan=False, description="总面积（亩）")


class FarmCreate(FarmBase, CoordinateInput):
    pass


class FarmUpdate(CoordinateInput):
    name: Optional[str] = PydanticField(None, min_length=1, max_length=100)
    description: Optional[str] = None
    province: Optional[str] = PydanticField(None, max_length=50)
    city: Optional[str] = PydanticField(None, max_length=50)
    district: Optional[str] = PydanticField(None, max_length=50)
    address: Optional[str] = PydanticField(None, max_length=255)
    region_code: Optional[str] = PydanticField(None, max_length=12)
    location_source: Optional[Literal['manual', 'region_reference']] = None
    location_label: Optional[str] = PydanticField(None, max_length=255)
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    area: Optional[float] = PydanticField(None, ge=0, allow_inf_nan=False)


class FarmResponse(FarmBase):
    archived_at: Optional[datetime] = None
    id: int
    owner_id: int
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class FarmListResponse(BaseModel):
    total: int
    items: List[FarmResponse]


# ==================== Field Schemas ====================

class FieldBase(BaseModel):
    facility_type: FacilityType = 'unknown'
    name: str = PydanticField(..., min_length=1, max_length=100, description="地块名称")
    area: float = PydanticField(0.0, ge=0, allow_inf_nan=False, description="地块面积（亩）")
    soil_type: Optional[str] = PydanticField(None, max_length=50, description="土壤类型，如壤土、沙壤土")
    irrigation_type: Optional[str] = PydanticField(None, max_length=50, description="灌溉方式，如滴灌、微喷灌")
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    remark: Optional[str] = None


class FieldCreate(FieldBase, CoordinateInput):
    pass


class FieldUpdate(CoordinateInput):
    facility_type: Optional[FacilityType] = None

    @model_validator(mode='before')
    @classmethod
    def explicit_facility(cls, data):
        if isinstance(data, dict) and 'facility_type' in data and data['facility_type'] is None:
            raise ValueError('设施类型不能为null；未核实请填unknown')
        return data

    name: Optional[str] = PydanticField(None, min_length=1, max_length=100)
    area: Optional[float] = PydanticField(None, ge=0, allow_inf_nan=False)
    soil_type: Optional[str] = PydanticField(None, max_length=50)
    irrigation_type: Optional[str] = PydanticField(None, max_length=50)
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    remark: Optional[str] = None


class FieldResponse(FieldBase):
    archived_at: Optional[datetime] = None
    id: int
    farm_id: int
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class FieldListResponse(BaseModel):
    total: int
    items: List[FieldResponse]


class ArchiveRequest(BaseModel):
    archived: bool


# ==================== Crop Planting Schemas ====================

class CropPlantingBase(BaseModel):
    crop: str = PydanticField(..., min_length=1, max_length=50, description="作物名称，如番茄")
    variety: Optional[str] = PydanticField(None, max_length=50, description="品种，如普罗旺斯")
    planting_date: date = PydanticField(..., description="定植/播种日期")
    expected_harvest_date: Optional[date] = PydanticField(None, description="预计采收日期")
    growth_stage: str = PydanticField("苗期", max_length=50, description="生长阶段：苗期/开花期/坐果期/采收期等")
    area: float = PydanticField(0.0, ge=0, allow_inf_nan=False, description="种植面积（亩）")
    status: str = PydanticField("active", description="active/harvested/terminated")


class CropInput(BaseModel):
    @model_validator(mode='before')
    @classmethod
    def required_crop_values(cls, data):
        if not isinstance(data, dict): return data
        data = dict(data)
        for key in ('crop', 'growth_stage', 'area', 'status', 'planting_date'):
            if key in data and data[key] is None: raise ValueError('作物、阶段、面积、状态和种植日期不能设为null')
        for key in ('crop', 'growth_stage'):
            if isinstance(data.get(key), str):
                data[key] = data[key].strip()
                if not data[key]: raise ValueError('作物和阶段不能仅为空白')
        return data


class CropPlantingCreate(CropPlantingBase, CropInput):
    status: Literal['active', 'harvested', 'terminated'] = 'active'

    @model_validator(mode='after')
    def valid_dates(self):
        if self.expected_harvest_date and self.expected_harvest_date < self.planting_date:
            raise ValueError('预计采收日期不能早于种植日期')
        return self


class CropPlantingUpdate(CropInput):
    crop: Optional[str] = PydanticField(None, min_length=1, max_length=50)
    variety: Optional[str] = PydanticField(None, max_length=50)
    planting_date: Optional[date] = None
    expected_harvest_date: Optional[date] = None
    growth_stage: Optional[str] = PydanticField(None, min_length=1, max_length=50)
    area: Optional[float] = PydanticField(None, ge=0, allow_inf_nan=False)
    status: Optional[Literal['active', 'harvested', 'terminated']] = None


class CropPlantingResponse(CropPlantingBase):
    id: int
    field_id: int
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class CropPlantingListResponse(BaseModel):
    total: int
    items: List[CropPlantingResponse]


# ==================== Farming Record Schemas ====================

class FarmingRecordBase(BaseModel):
    request_id: Optional[UUID] = None
    record_type: str = PydanticField(..., description="农事类型：播种/移栽/施肥/灌溉/打药/修剪/采收/除草等")
    record_date: date = PydanticField(..., description="农事操作日期")
    crop_planting_id: Optional[int] = PydanticField(None, description="关联种植批次ID")
    operator: Optional[str] = PydanticField(None, max_length=100, description="作业人员")
    description: Optional[str] = PydanticField(None, description="详细说明")
    input_name: Optional[str] = PydanticField(None, max_length=100, description="投入品名称")
    input_amount: Optional[float] = PydanticField(None, ge=0, allow_inf_nan=False, description="投入品数量")
    unit: Optional[str] = PydanticField(None, max_length=20, description="单位")
    safety_interval_days: Optional[int] = PydanticField(None, ge=0, le=120, description="农药安全间隔期 (PHI, 天)")
    attachments: List[Any] = PydanticField(default_factory=list, description="图片或附件")


class FarmingRecordCreate(FarmingRecordBase):
    pass


class FarmingRecordCreateRequest(BaseModel):
    """用于 /api/farming-records 兼容端点的结构化强类型请求模型（彻底摒弃裸 dict）"""
    field_id: int = PydanticField(..., description="地块ID")
    request_id: Optional[UUID] = None
    record_type: str = PydanticField("other", description="农事类型：播种/移栽/施肥/灌溉/打药/修剪/采收/除草等")
    record_date: Optional[date] = PydanticField(None, description="农事操作日期")
    operation_date: Optional[date] = PydanticField(None, description="农业业务日期YYYY-MM-DD；无效日期拒绝写入")
    crop_planting_id: Optional[int] = PydanticField(None, description="关联种植批次ID")
    operator: Optional[str] = PydanticField(None, max_length=100, description="作业人员")
    description: Optional[str] = PydanticField(None, description="详细说明")
    operation_content: Optional[str] = PydanticField(None, description="兼容旧字段 operation_content")
    input_name: Optional[str] = PydanticField(None, max_length=100, description="投入品名称")
    chemical_name: Optional[str] = PydanticField(None, max_length=100, description="兼容旧字段 chemical_name")
    dosage: Optional[str] = PydanticField(None, max_length=50, description="兼容旧用药剂量字符串，如 100ml")
    input_amount: Optional[float] = PydanticField(None, ge=0, allow_inf_nan=False, description="投入品数量")
    unit: Optional[str] = PydanticField(None, max_length=20, description="单位")
    safety_interval_days: Optional[int] = PydanticField(None, ge=0, le=120, description="农药安全间隔期 (PHI, 天)")
    attachments: List[Any] = PydanticField(default_factory=list, description="图片或附件")


class FarmingRecordUpdate(BaseModel):
    record_type: Optional[str] = None
    record_date: Optional[date] = None
    crop_planting_id: Optional[int] = None
    operator: Optional[str] = None
    description: Optional[str] = None
    input_name: Optional[str] = None
    input_amount: Optional[float] = None
    unit: Optional[str] = None
    safety_interval_days: Optional[int] = None
    attachments: Optional[List[Any]] = None


class FarmingRecordResponse(FarmingRecordBase):
    attachments: List[Any] | dict[str, Any] = PydanticField(default_factory=list)
    id: int
    field_id: int
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    can_delete: bool = False
    delete_deadline_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None
    revoke_reason: Optional[str] = None
    revoked_by_name: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class FarmingRecordListResponse(BaseModel):
    total: int
    items: List[FarmingRecordResponse]


# ==================== Disease Record Schemas ====================

class DiseaseRecordBase(BaseModel):
    disease_name: str = PydanticField(..., min_length=1, max_length=100, description="病害名称")
    crop_planting_id: Optional[int] = None
    symptoms: Optional[str] = PydanticField(None, description="症状描述")
    severity: str = PydanticField("medium", description="危害严重程度: low, medium, high, severe")
    diagnosis_method: str = PydanticField("ai_vision", description="诊断方式: ai_vision, manual, expert")
    diagnosis_confidence: Optional[float] = PydanticField(None, ge=0.0, le=1.0, description="AI置信度")
    image_url: Optional[str] = PydanticField(None, max_length=500, description="病害图片地址")
    expert_result: Optional[str] = None
    status: str = PydanticField("preliminary", description="preliminary / expert_reviewed / resolved")


class DiseaseRecordCreate(DiseaseRecordBase):
    severity: Literal["unknown", "low", "medium", "high", "severe"] = "unknown"
    diagnosis_method: Literal["manual"] = "manual"
    diagnosis_confidence: Literal[None] = None
    expert_result: Literal[None] = None
    status: Literal["preliminary", "pending_review"] = "preliminary"


class DiseaseRecordUpdate(BaseModel):
    disease_name: Optional[str] = None
    symptoms: Optional[str] = None
    severity: Optional[str] = None
    diagnosis_confidence: Optional[float] = None
    expert_result: Optional[str] = None
    status: Optional[str] = None


class DiseaseRecordResponse(DiseaseRecordBase):
    id: int
    field_id: int
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class DiseaseRecordListResponse(BaseModel):
    total: int
    items: List[DiseaseRecordResponse]


# ==================== Weather Snapshot Schemas ====================

class WeatherSnapshotBase(BaseModel):
    timestamp: datetime = PydanticField(default_factory=datetime.utcnow, description="观测/预报时间")
    temperature: Optional[float] = PydanticField(None, description="气温(℃)")
    humidity: Optional[float] = PydanticField(None, ge=0, le=100, description="相对湿度(%)")
    rainfall: float = PydanticField(0.0, ge=0, description="降水量(mm)")
    wind_speed: Optional[float] = PydanticField(None, ge=0, description="风速(m/s)")
    weather: Optional[str] = PydanticField(None, description="天气现象，如晴、多云、中雨")
    source: str = PydanticField("api", description="数据来源")


class WeatherSnapshotCreate(WeatherSnapshotBase):
    pass


class WeatherSnapshotResponse(WeatherSnapshotBase):
    id: int
    field_id: int
    created_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class WeatherSnapshotListResponse(BaseModel):
    total: int
    items: List[WeatherSnapshotResponse]


# ==================== Expert Case Schemas ====================

class ExpertCaseCreate(BaseModel):
    disease_record_id: int
    confirmed_disease: str = PydanticField(..., min_length=1, max_length=100)
    treatment_suggestion: str = PydanticField(..., min_length=5, max_length=6000)
    notes: Optional[str] = PydanticField(None, max_length=4000)
    is_knowledge_candidate: int = PydanticField(0, ge=0, le=1)


class ExpertCaseUpdate(BaseModel):
    confirmed_disease: Optional[str] = None
    treatment_suggestion: Optional[str] = None
    notes: Optional[str] = None
    status: Optional[str] = None
    is_knowledge_candidate: Optional[int] = None


class ExpertCaseResponse(BaseModel):
    id: int
    disease_record_id: int
    expert_id: Optional[int] = None
    confirmed_disease: str
    treatment_suggestion: str
    notes: Optional[str] = None
    status: str
    is_knowledge_candidate: int
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


# ==================== Agricultural Plan Schemas ====================

class PlanItem(BaseModel):
    date: str = PydanticField(..., description="计划执行日期 (YYYY-MM-DD)")
    record_type: str = PydanticField(..., description="农事类型：irrigation/fertilization/pest_control/weeding/harvesting/field_management")
    action: str = PydanticField(..., description="操作名称与动作")
    description: str = PydanticField(..., description="操作详细说明与配方")
    reason: str = PydanticField(..., description="生成理由依据（物候与气象）")
    source: str = PydanticField(..., description="规程知识来源与标准")
    risk_notes: Optional[str] = PydanticField(None, description="风险注意事项与安全间隔期提示")
    is_spray: bool = PydanticField(False, description="是否涉及施药/打药作业")
    can_proceed: bool = PydanticField(True, description="是否适宜作业（无恶劣气象阻断）")
    reason_code: str = "review_required"
    crop_planting_id: Optional[int] = None
    proof: Optional[str] = PydanticField(None, max_length=100, description="服务器确认凭证，有效期15分钟")
    execution_status: Literal['pending', 'completed', 'revoked', 'unavailable'] = 'pending'
    execution_record_id: Optional[int] = None


class AgriPlanRequest(BaseModel):
    field_id: int = PydanticField(..., description="农田地块 ID")
    crop_planting_id: Optional[int] = PydanticField(None, description="种植作物批次 ID（不传则自动取地块当前活跃批次）")
    days: int = PydanticField(7, ge=1, le=14, description="规划时间窗口（天数，默认 7 天）")
    focus_topic: Optional[str] = PydanticField(None, description="关注主题：disease/pest/fertilizer/irrigation/all")
    force_refresh: bool = PydanticField(False, description="重新核查计划和天气；默认复用有效的已保存计划")


class AgriPlanResponse(BaseModel):
    field_id: int
    field_name: str
    crop: str
    growth_stage: str
    start_date: str
    end_date: str
    summary: str
    items: List[PlanItem]
    weather_summary: str
    total_tasks: int
    version: int = 0
    generated_at: Optional[datetime] = None
    evaluated_at: Optional[datetime] = None
    weather_fetched_at: Optional[str] = None
    weather_observed_at: Optional[str] = None
    update_reason: str = ''
    changes: List[str] = PydanticField(default_factory=list)


class AgriPlanApplyRequest(BaseModel):
    field_id: int = PydanticField(..., description="农田地块 ID")
    items: List[PlanItem] = PydanticField(..., min_length=1, description="待落库沉淀的农事计划项列表")
    operator: Optional[str] = PydanticField("农场管理员", max_length=100, description="作业执行人员")


class AgriPlanApplyResponse(BaseModel):
    success: bool
    created_count: int
    record_ids: List[int]
    message: str

