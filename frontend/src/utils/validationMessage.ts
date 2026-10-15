const labels: Record<string, string> = {
  product_name: '产品全名', registration_number: '登记证号', applicable_crop: '标签适用作物',
  label_reference: '标签依据', usage_scope: '标签用途与限制', phi_days: '标签PHI',
  valid_until: '复核截止时间', planned_operation_date: '拟作业日期', planned_harvest_date: '计划采收日期',
  context_fingerprint: '复核资料快照', review_notes: '复核理由', evidence_references: '核验依据',
  observed_at: '观测时间', measurement_scope: '观测位置', facility_type: '设施类型',
  temperature: '实测温度', humidity: '实测湿度', ventilation: '通风状态', covering: '覆盖状态',
  operation_date: '作业日期', record_date: '作业日期', planting_date: '定植日期',
  expected_harvest_date: '预计采收日期', area: '面积', crop_planting_id: '作物批次',
  field_id: '田块', safety_interval_days: '安全间隔期', status: '状态',
  effective_from: '生效日期', effective_to: '失效日期', latitude: '纬度', longitude: '经度'
}

export function validationMessage(detail: unknown): string {
  if (typeof detail === 'string') return detail.slice(0, 200)
  if (!Array.isArray(detail)) return '填写内容有误，请检查必填项和格式'
  const fields = [...new Set(detail.map(item => {
    if (!Array.isArray(item?.loc)) return '表单'
    const field = String(item.loc.at(-1))
    return labels[field] || '表单'
  }))]
  return `${fields.slice(0, 4).join('、')}填写有误，请检查日期、范围和必填项`
}
