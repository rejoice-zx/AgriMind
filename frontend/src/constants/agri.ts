/**
 * 农业业务枚举与展示映射（全站唯一来源）。
 *
 * 后端 record_type 是自由文本，历史数据里混有中英文同义值；这里只做"展示归类"，
 * 不改写已存数据。归类规则与 backend/app/models/farming_record.py 的类型集合保持一致。
 */

export type TagType = 'primary' | 'success' | 'warning' | 'danger' | 'info'

export interface RecordKind {
  key: 'spraying' | 'fertilization' | 'irrigation' | 'field_management' | 'weeding' | 'harvest' | 'inspection' | 'other'
  label: string
  tag: TagType
}

const KINDS: Record<RecordKind['key'], RecordKind> = {
  spraying: { key: 'spraying', label: '植保施药', tag: 'danger' },
  fertilization: { key: 'fertilization', label: '施肥', tag: 'warning' },
  irrigation: { key: 'irrigation', label: '灌溉', tag: 'primary' },
  field_management: { key: 'field_management', label: '田间管理', tag: 'success' },
  weeding: { key: 'weeding', label: '除草', tag: 'info' },
  harvest: { key: 'harvest', label: '采收', tag: 'success' },
  inspection: { key: 'inspection', label: '现场检查（非作业实施）', tag: 'info' },
  other: { key: 'other', label: '其他农事', tag: 'info' },
}

const ALIASES: Array<[RecordKind['key'], string[]]> = [
  ['spraying', ['spraying', 'pest_control', '施药', '打药', '喷药', '植保', '病虫害防治']],
  ['fertilization', ['fertilization', 'fertilizing', '施肥', '追肥', '水肥管理']],
  ['irrigation', ['irrigation', '灌溉', '浇水']],
  ['field_management', ['field_management', 'pruning', '田间管理', '整枝', '抹杈', '授粉', '修剪']],
  ['weeding', ['weeding', '除草']],
  ['harvest', ['harvest', 'harvesting', '采收']],
  ['inspection', ['inspection', '巡视', '检查']],
]

/** 把任意历史 record_type 归入展示类别；未知值保留原文，不伪装成已知类型。 */
export function recordKind(type?: string | null): RecordKind {
  const value = (type || '').trim().toLowerCase()
  for (const [key, names] of ALIASES) {
    if (names.some(name => value === name.toLowerCase())) return KINDS[key]
  }
  for (const [key, names] of ALIASES) {
    if (key !== 'inspection' && names.some(name => /[一-鿿]/.test(name) && value.includes(name))) return KINDS[key]
  }
  return value ? { ...KINDS.other, label: type!.trim() } : KINDS.other
}

export const isSprayRecord = (type?: string | null) => recordKind(type).key === 'spraying'

/** 新增农事记录时可选的类型（写入值沿用前端现有的英文键，后端按同义词集合识别）。 */
export const RECORD_TYPE_OPTIONS: Array<{ value: string; label: string }> = [
  { value: 'spraying', label: '植保施药（防病/治虫）' },
  { value: 'fertilization', label: '施肥（追肥/水溶肥）' },
  { value: 'irrigation', label: '灌溉（滴灌/微喷/浇水）' },
  { value: 'field_management', label: '田间管理（整枝/打杈/授粉）' },
  { value: 'weeding', label: '除草' },
  { value: 'harvest', label: '采收' },
  { value: 'other', label: '其他农事' },
]

/** 安全间隔期输入上限（天）。只是输入防错范围，真实值以产品标签为准。 */
export const PHI_MAX_DAYS = 90

/** 生育期选项：值与知识库检索的同义组对齐（backend/app/core/knowledge_contract.py STAGE_GROUPS）。 */
export const GROWTH_STAGE_OPTIONS: Array<{ value: string; label: string }> = [
  { value: '待核实', label: '待核实' },
  { value: '苗期', label: '苗期 / 缓苗期' },
  { value: '开花坐果期', label: '开花坐果期' },
  { value: '果实膨大期', label: '果实膨大期' },
  { value: '采收期', label: '采收期' },
  { value: '全生育期', label: '全生育期（通用）' },
]

/** 病害工单（初诊复核）状态：初诊页与历史面板共用，避免"确诊出方"等过度承诺。 */
export const TICKET_STATUS: Record<string, { label: string; tag: TagType }> = {
  preliminary: { label: '仅初诊，未申请复核', tag: 'info' },
  pending_review: { label: '待专家接单', tag: 'warning' },
  in_review: { label: '专家处理中', tag: 'primary' },
  expert_reviewed: { label: '专家已复核', tag: 'success' },
  resolved: { label: '已结案', tag: 'success' },
}

export function ticketStatus(status?: string | null) {
  return TICKET_STATUS[status || ''] || { label: status || '未知状态', tag: 'info' as TagType }
}

export const ROLE_LABELS: Record<string, string> = {
  admin: '管理员',
  expert: '农技专家',
  customer: '农户',
  cs: '早期客服（已停用）',
}

export const roleLabel = (role?: string | null) => ROLE_LABELS[role || ''] || '未知角色'

/** 数据来源分类：界面上统一用 SourceBadge 区分，不让预报、观测、AI 与专家结论混在一起。 */
export type SourceKind = 'forecast' | 'observation' | 'record' | 'ai' | 'expert' | 'knowledge'

export const SOURCE_LABELS: Record<SourceKind, string> = {
  forecast: '室外预报',
  observation: '现场观测',
  record: '档案记录',
  ai: '模型生成',
  expert: '专家签署',
  knowledge: '知识库候选',
}
