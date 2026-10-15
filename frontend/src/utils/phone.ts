export function normalizePhone(value: string): string {
  return value.trim().replace(/[ ()-]/g, '')
}

export function validPhone(value: string): boolean {
  return /^\+?[0-9 ()-]+$/.test(value.trim()) && /^\+?\d{7,15}$/.test(normalizePhone(value))
}

export const phoneRules = [
  { required: true, message: '请输入联系电话', trigger: 'blur' },
  { validator: (_rule: unknown, value: string, callback: (error?: Error) => void) => {
    callback(typeof value === 'string' && validPhone(value) ? undefined : new Error('请输入7至15位联系电话，可含+区号、空格或短横线'))
  }, trigger: 'blur' }
]
