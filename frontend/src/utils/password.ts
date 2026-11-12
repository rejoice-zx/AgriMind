/**
 * 新密码规则，与后端 PasswordText 一致：至少 6 个字符，UTF-8 编码后不超过 72 字节（bcrypt 上限）。
 * 中文字符占 3 字节，所以纯中文密码最多约 24 个字。
 */
export const PASSWORD_MIN = 6
export const PASSWORD_MAX_BYTES = 72
export const PASSWORD_HINT = '至少 6 个字符，最长 72 字节（约 72 个英文字符或 24 个汉字）'

export function passwordProblem(value: string): string {
  if (!value) return '请输入密码'
  if (value.length < PASSWORD_MIN) return `密码至少 ${PASSWORD_MIN} 个字符`
  if (new TextEncoder().encode(value).length > PASSWORD_MAX_BYTES) return '密码过长：不能超过 72 字节（约 24 个汉字）'
  return ''
}

export const passwordRules = [
  { required: true, message: '请输入密码', trigger: 'blur' },
  { validator: (_rule: unknown, value: string, callback: (error?: Error) => void) => {
    const problem = passwordProblem(value || '')
    callback(problem ? new Error(problem) : undefined)
  }, trigger: 'blur' }
]
