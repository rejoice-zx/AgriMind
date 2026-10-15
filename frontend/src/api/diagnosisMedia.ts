import request from '@/utils/request'

// Never forward the session header to an arbitrary URL supplied through a record.
export function readDiagnosisImage(url: string) {
  const valid = /^\/api\/diagnosis\/media\/[0-9a-f]{32}\.(jpeg|jpg|png|webp)$/.test(url) ||
    /^\/static\/uploads\/diagnosis\/[A-Za-z0-9_-][A-Za-z0-9_.-]{0,99}\.(jpeg|jpg|png|webp)$/.test(url)
  if (!valid) return Promise.reject(new Error('图片地址不可用，请重新上传'))
  return request.get<Blob>(url, { baseURL: '/', responseType: 'blob' })
}
